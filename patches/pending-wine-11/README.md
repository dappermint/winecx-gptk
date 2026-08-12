# metal cluster, not yet on wine 11

Held out of the build. `patches/*.patch` is the apply glob, so nothing here runs.

These four are the cross-process layer bridge: a process that renders into
another process's window publishes its metal layer as a `CAContext` and the
owning process hosts it with a `CALayerHost`. Steam's webhelper is the case
that needs it.

## where the port stands

`WIP-0003-cocoa-half-ported.diff` is the Cocoa half of 0003 already resolved
against 26.3: `cocoa_window.m` and `macdrv_cocoa.h`, 361 lines. It is pure
Cocoa and carried over with three trivial conflicts, all of them 26.3
retyping something next to our insertion point:

  - `macdrv_get_view_backing_size` returns `bool`, was `int`
  - `setLayerRetinaProperties:` takes `BOOL`, was `int`
  - a new `d3dmetal_client_surface` ivar sits where ours goes

## what still needs writing, and why it is not a rebase

Wine 11 replaced the per-driver surface plumbing with one `client_surface`
object shared by opengl, vulkan and d3dmetal.

`macdrv_client_surface_create` no longer needs `get_win_data(hwnd)` to
succeed: it makes a standalone view. That was the exact failure 0003 was
written for, so the create-time half of the problem is fixed upstream.
`macdrv_client_surface_update` still resolves the toplevel through
`get_win_data`, so a cross-process toplevel should still leave the view
unattached. Unconfirmed, and worth measuring before writing anything.

- **vulkan.c** — do not port. Wine 11 rewrote it from ~250 lines to 141 and
  the hunks have nothing to attach to. The one hook that serves all three
  clients is `macdrv_client_surface_update`, which is better than what we
  had: the old patches had to touch vulkan.c and d3dmetal.c separately.
- **window.c** — 0003's hunk applies cleanly and does not compile. It calls
  `data->client_cocoa_view` and `create_client_cocoa_view`, both of which
  wine 11 removed; the field is now `client_view` and it belongs to whichever
  `client_surface` presented last. The receiving side needs a view of its own
  rather than borrowing that one.
- **d3dmetal.c** — 26.3 has its own, built on `client_surface` and
  `macdrv_set_view_d3dmetal_client_surface`. Overlapping ground, needs
  reading before merging.

Pristine 26.3 has no `CAContext`, `CALayerHost`, remote layer or swapchain of
its own, so the mechanism itself is still entirely ours.
