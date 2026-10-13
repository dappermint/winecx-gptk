# Metal cache regression tests

These standalone tests exercise the experimental KosmicKrisp cache using fake
Metal objects and an in-memory disk cache. They do not launch Wine or a game.
They require a configured Mesa checkout providing `kk_metal_cache.c`, including
the uncaptured compiler, recapture queue and archive budget interfaces. The
runtime's older pinned Mesa is not the test target.

```sh
python3 tests/run_kosmickrisp_metal_cache.py \
  --mesa /path/to/mesa --build /path/to/mesa/build \
  --out /tmp/kk-cache-tests
```

Use an empty output directory. Select individual cases with repeated
`--scenario` arguments. The build configuration determines the architecture;
an x86_64 build can run under Rosetta on Apple Silicon.

The 31 scenarios cover concurrent archive access, serialization failure and
retry, captured/uncaptured compilation, orderly and fast process exit, cache
capacity, and shutdown. Late recapture tests hold an uncaptured compile until
a serializer flush has finished, then require it to be captured without another
explicit flush. Shutdown variants require the resulting archive to be reusable
after cache destruction and recreation. Both render and compute paths are
covered. Mock tests do not establish Metal service behavior or game correctness.
