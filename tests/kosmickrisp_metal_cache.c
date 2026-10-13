#include "kosmickrisp/vulkan/kk_metal_cache.h"
#include "kosmickrisp/bridge/mtl_bridge.h"
#include "util/u_debug.h"
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>
#include <time.h>

#undef atexit
#undef calloc


#define WORDS 32

struct payload {
   uint64_t magic;
   uint64_t ids[WORDS];
};
struct stored {
   cache_key key;
   void *data;
   size_t size;
   struct stored *next;
};
struct fake_object {
   atomic_int refs;
   bool compiler;
   struct payload payload;
};
struct pipeline_task {
   struct kk_metal_cache *cache;
   unsigned id;
   bool expect_hit;
   unsigned repetitions;
};

static struct stored *items;
static atomic_uint builds;
static atomic_uint snapshots;
static atomic_uint archive_loads;
static atomic_uint live_objects;
static atomic_uint uncaptured_objects;
static atomic_uint uncaptured_builds;
static bool fail_uncaptured;
static atomic_bool fail_exit_registration;
static atomic_bool fail_exit_snapshot;
static void (*registered_exit)(void);
static bool hold_disk;
static bool disk_entered;
static bool allow_disk;
static atomic_bool destroy_finished;
static unsigned held_archive_id;
static bool archive_entered;
static bool allow_archive;
static pthread_mutex_t storage_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t capture_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t control_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t control_cond = PTHREAD_COND_INITIALIZER;
static struct payload captured = {.magic = 0xfade0123456789abULL};
static unsigned blocked_id;
static unsigned failed_compile_id;
static bool compile_entered;
static bool allow_compile;
static bool hold_snapshot;
static bool snapshot_entered;
static bool allow_snapshot;
static bool fail_snapshot;
static bool snapshot_completed;

int kk_test_atexit(void (*callback)(void))
{
   if (atomic_load(&fail_exit_registration))
      return 1;
   registered_exit = callback;
   return atexit(callback);
}

void *kk_test_calloc(size_t count, size_t size)
{
   if (atomic_load(&fail_exit_snapshot) && size == sizeof(void *))
      return NULL;
   return calloc(count, size);
}

static void key_for_id(unsigned id, cache_key key)
{
   memset(key, 0, CACHE_KEY_SIZE);
   memcpy(key, &id, sizeof(id));
}

static void *object(const struct payload *payload)
{
   struct fake_object *obj = calloc(1, sizeof(*obj));
   assert(obj);
   atomic_init(&obj->refs, 1);
   atomic_fetch_add(&live_objects, 1);
   if (payload)
      obj->payload = *payload;
   return obj;
}

void *mtl_retain(void *p)
{
   if ((uintptr_t)p > WORDS * 64)
      atomic_fetch_add(&((struct fake_object *)p)->refs, 1);
   return p;
}

void mtl_release(void *p)
{
   if ((uintptr_t)p > WORDS * 64 && atomic_fetch_sub(&((struct fake_object *)p)->refs, 1) == 1) {
      if (((struct fake_object *)p)->compiler)
         atomic_fetch_sub(&uncaptured_objects, 1);
      atomic_fetch_sub(&live_objects, 1);
      free(p);
   }
}

static unsigned pipeline_objects(void)
{
   return atomic_load(&live_objects) - atomic_load(&uncaptured_objects);
}

mtl_compiler *mtl_new_uncaptured_compiler(mtl_device *device)
{
   if (fail_uncaptured)
      return NULL;
   struct fake_object *compiler = object(NULL);
   compiler->compiler = true;
   atomic_fetch_add(&uncaptured_objects, 1);
   return compiler;
}

const char *debug_get_option(const char *name, const char *def)
{
   return getenv(name) ? getenv(name) : def;
}

bool debug_get_bool_option(const char *name, bool def)
{
   return getenv(name) ? !strcmp(getenv(name), "1") : def;
}

void disk_cache_compute_key(struct disk_cache *cache, const void *data,
                            size_t size, cache_key key)
{
   uint64_t hashes[4] = {14695981039346656037ULL, 0x12345678ULL,
                         0xabcdef23ULL, 0xfedcba987ULL};
   for (size_t i = 0; i < size; i++)
      for (unsigned j = 0; j < 4; j++)
         hashes[j] = (hashes[j] ^ (((const unsigned char *)data)[i] + j)) *
                     1099511628211ULL;
   memcpy(key, hashes, CACHE_KEY_SIZE);
}

void disk_cache_put(struct disk_cache *cache, const cache_key key,
                    const void *data, size_t size, struct cache_item_metadata *meta)
{
   pthread_mutex_lock(&storage_lock);
   struct stored *entry;
   for (entry = items; entry; entry = entry->next)
      if (!memcmp(entry->key, key, CACHE_KEY_SIZE))
         break;
   if (!entry) {
      entry = calloc(1, sizeof(*entry));
      assert(entry);
      memcpy(entry->key, key, CACHE_KEY_SIZE);
      entry->next = items;
      items = entry;
   }
   free(entry->data);
   entry->data = malloc(size);
   assert(entry->data);
   memcpy(entry->data, data, size);
   entry->size = size;
   pthread_mutex_unlock(&storage_lock);
}

void *disk_cache_get(struct disk_cache *cache, const cache_key key, size_t *size)
{
   pthread_mutex_lock(&storage_lock);
   void *result = NULL;
   for (struct stored *entry = items; entry; entry = entry->next) {
      if (memcmp(entry->key, key, CACHE_KEY_SIZE))
         continue;
      *size = entry->size;
      result = malloc(*size);
      assert(result);
      memcpy(result, entry->data, *size);
      break;
   }
   pthread_mutex_unlock(&storage_lock);
   return result;
}

void disk_cache_wait_for_idle(struct disk_cache *cache)
{
   pthread_mutex_lock(&control_lock);
   disk_entered = true;
   pthread_cond_broadcast(&control_cond);
   while (hold_disk && !allow_disk)
      pthread_cond_wait(&control_cond, &control_lock);
   pthread_mutex_unlock(&control_lock);
   const char *path = getenv("KK_TEST_EXIT_MARKER");
   if (path) {
      pthread_mutex_lock(&storage_lock);
      unsigned count = 0;
      for (struct stored *entry = items; entry; entry = entry->next)
         count++;
      FILE *file = fopen(path, "w");
      assert(file);
      fprintf(file, "{\"snapshots\":%u,\"entries\":%u}\n",
              atomic_load(&snapshots), count);
      assert(!fclose(file));
      pthread_mutex_unlock(&storage_lock);
   }
}

bool mtl_compiler_has_serializer(mtl_compiler *compiler)
{
   return true;
}

bool mtl_compile_phase_enabled(void)
{
   return false;
}

struct mtl_compile_phase mtl_compile_phase_begin(const char *operation, const void *object,
                                                 const char *key, const char *origin)
{
   return (struct mtl_compile_phase){0};
}

void mtl_compile_phase_end(struct mtl_compile_phase phase, bool success)
{
}

void mtl_pipeline_cache_identity(mtl_device *device, uint32_t gpu_family, char *identity, size_t size)
{
   snprintf(identity, size, "test-device-1");
}

void *mtl_compiler_serialize(mtl_compiler *compiler, size_t *size)
{
   pthread_mutex_lock(&control_lock);
   snapshot_entered = true;
   pthread_cond_broadcast(&control_cond);
   while (hold_snapshot && !allow_snapshot)
      pthread_cond_wait(&control_cond, &control_lock);
   bool fail = fail_snapshot;
   fail_snapshot = false;
   pthread_mutex_unlock(&control_lock);

   struct payload *data = malloc(sizeof(*data));
   assert(data);
   pthread_mutex_lock(&capture_lock);
   *data = captured;
   if (!fail)
      memset(captured.ids, 0, sizeof(captured.ids));
   pthread_mutex_unlock(&capture_lock);
   atomic_fetch_add(&snapshots, 1);
   *size = sizeof(*data);

   pthread_mutex_lock(&control_lock);
   snapshot_completed = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   if (fail) {
      free(data);
      *size = 0;
      return NULL;
   }
   return data;
}

mtl_archive *mtl_new_archive(mtl_device *device, const void *data, size_t size)
{
   const struct payload *payload = data;
   atomic_fetch_add(&archive_loads, 1);
   return size == sizeof(*payload) && payload->magic == captured.magic ?
      object(payload) : NULL;
}

static void *from_archive(mtl_archive *archive, unsigned id)
{
   assert(id < WORDS * 64);
   pthread_mutex_lock(&control_lock);
   if (id == held_archive_id) {
      archive_entered = true;
      pthread_cond_broadcast(&control_cond);
      while (!allow_archive)
         pthread_cond_wait(&control_cond, &control_lock);
   }
   pthread_mutex_unlock(&control_lock);
   struct fake_object *obj = archive;
   return obj->payload.ids[id / 64] & (1ULL << (id % 64)) ? object(NULL) : NULL;
}

mtl_render_pipeline_state *mtl_archive_new_render_pipeline(
   mtl_archive *archive, mtl_render_pipeline_descriptor *descriptor)
{
   return from_archive(archive, (unsigned)(uintptr_t)descriptor);
}

mtl_compute_pipeline_state *mtl_archive_new_compute_pipeline(
   mtl_archive *archive, mtl_function_descriptor *function, uint64_t max_threads)
{
   return from_archive(archive, (unsigned)(uintptr_t)function);
}

static void *compile(mtl_compiler *compiler, unsigned id)
{
   assert(id < WORDS * 64);
   pthread_mutex_lock(&control_lock);
   if (id == blocked_id) {
      compile_entered = true;
      pthread_cond_broadcast(&control_cond);
      while (!allow_compile)
         pthread_cond_wait(&control_cond, &control_lock);
   }
   pthread_mutex_unlock(&control_lock);
   bool success = id != failed_compile_id;
   if (compiler == (void *)3 && success) {
      pthread_mutex_lock(&capture_lock);
      captured.ids[id / 64] |= 1ULL << (id % 64);
      pthread_mutex_unlock(&capture_lock);
   } else if (compiler != (void *)3) {
      atomic_fetch_add(&uncaptured_builds, 1);
   }
   atomic_fetch_add(&builds, 1);
   return success ? object(NULL) : NULL;
}

mtl_render_pipeline_state *mtl_new_render_pipeline(
   mtl_compiler *compiler, mtl_render_pass_descriptor *descriptor)
{
   return compile(compiler, (unsigned)(uintptr_t)descriptor);
}

mtl_compute_pipeline_state *mtl_new_compute_pipeline_state(
   mtl_compiler *compiler, mtl_function_descriptor *function, uint64_t max_threads)
{
   return compile(compiler, (unsigned)(uintptr_t)function);
}

static struct kk_metal_cache *create(void)
{
   struct kk_metal_cache *cache = kk_metal_cache_create((void *)1, (void *)2, 0, (void *)3, 0);
   assert(cache);
   return cache;
}

static void pipeline(struct kk_metal_cache *cache, unsigned id, bool expect_hit)
{
   cache_key key;
   key_for_id(id, key);
   bool hit = false;
   void *result = id % 2 ?
      kk_metal_cache_new_render_pipeline(cache, key, (void *)(uintptr_t)id, &hit) :
      kk_metal_cache_new_compute_pipeline(cache, key, (void *)(uintptr_t)id, 64, &hit);
   if (!result || hit != expect_hit) {
      fprintf(stderr, "pipeline %u: expected archive_hit=%d, observed=%d\n",
              id, expect_hit, hit);
      abort();
   }
   mtl_release(result);
}

static void *pipeline_thread(void *arg)
{
   struct pipeline_task *task = arg;
   for (unsigned i = 0; i < task->repetitions; i++)
      pipeline(task->cache, task->id, task->expect_hit);
   return NULL;
}

static void *flush_thread(void *arg)
{
   kk_metal_cache_flush(arg);
   return NULL;
}

static bool wait_flag(bool *flag, unsigned seconds)
{
   struct timespec until;
   clock_gettime(CLOCK_REALTIME, &until);
   until.tv_sec += seconds;
   while (!*flag)
      if (pthread_cond_timedwait(&control_cond, &control_lock, &until))
         return false;
   return true;
}

static void race_test(unsigned count)
{
   struct kk_metal_cache *cache = create();
   blocked_id = 1000;
   hold_snapshot = true;
   struct pipeline_task task = {cache, blocked_id, false, 1};
   pthread_t thread;
   assert(!pthread_create(&thread, NULL, pipeline_thread, &task));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&compile_entered, 5));
   pthread_mutex_unlock(&control_lock);
   for (unsigned i = 1; i <= count; i++)
      pipeline(cache, i, false);
   pthread_t idle;
   assert(!pthread_create(&idle, NULL, flush_thread, cache));
   pthread_mutex_lock(&control_lock);
   bool snapshot_overlapped = wait_flag(&snapshot_entered, 1);
   allow_compile = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(thread, NULL);
   pthread_mutex_lock(&control_lock);
   allow_snapshot = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(idle, NULL);
   kk_metal_cache_destroy(cache);
   cache = create();
   pipeline(cache, blocked_id, true);
   for (unsigned i = 1; i <= count; i++)
      pipeline(cache, i, true);
   kk_metal_cache_destroy(cache);
   assert(!snapshot_overlapped);
}

static void serialization_test(void)
{
   struct kk_metal_cache *cache = create();
   hold_snapshot = true;
   pipeline(cache, 1, false);
   pthread_t idle;
   assert(!pthread_create(&idle, NULL, flush_thread, cache));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&snapshot_entered, 5));
   blocked_id = 2;
   pthread_mutex_unlock(&control_lock);
   struct pipeline_task task = {cache, 2, false, 1};
   pthread_t thread;
   assert(!pthread_create(&thread, NULL, pipeline_thread, &task));
   pthread_mutex_lock(&control_lock);
   bool compile_overlapped = wait_flag(&compile_entered, 1);
   pthread_mutex_unlock(&control_lock);
   pipeline(cache, 5, false);
   assert(atomic_load(&uncaptured_builds) == 1);
   pthread_mutex_lock(&control_lock);
   allow_snapshot = true;
   allow_compile = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(thread, NULL);
   pthread_join(idle, NULL);
   kk_metal_cache_destroy(cache);
   assert(compile_overlapped);
   assert(atomic_load(&uncaptured_builds) == 2);
   assert(atomic_load(&builds) == 5);
   cache = create();
   pipeline(cache, 1, true);
   pipeline(cache, 2, true);
   pipeline(cache, 5, true);
   kk_metal_cache_destroy(cache);
}

static void failure_test(void)
{
   struct kk_metal_cache *cache = create();
   fail_snapshot = true;
   pipeline(cache, 1, false);
   kk_metal_cache_flush(cache);
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&snapshot_completed, 5));
   pthread_mutex_unlock(&control_lock);
   pipeline(cache, 2, false);
   kk_metal_cache_destroy(cache);
   uint8_t input[256 + CACHE_KEY_SIZE] = {0};
   snprintf((char *)input, 256, "test-device-1");
   cache_key key, reference;
   key_for_id(1, key);
   memcpy(input + 256, key, sizeof(key));
   disk_cache_compute_key((void *)1, input, sizeof(input), reference);
   size_t size;
   void *ref = disk_cache_get((void *)1, reference, &size);
   assert(ref && size == CACHE_KEY_SIZE);
   free(ref);
   cache = create();
   pipeline(cache, 2, true);
   pipeline(cache, 1, true);
   kk_metal_cache_destroy(cache);
   cache = create();
   pipeline(cache, 1, true);
   kk_metal_cache_destroy(cache);
}

static void concurrency_test(void)
{
   struct kk_metal_cache *cache = create();
   struct pipeline_task tasks[16];
   pthread_t threads[16];
   for (unsigned i = 0; i < 16; i++) {
      tasks[i] = (struct pipeline_task){cache, i + 1, false, 1};
      assert(!pthread_create(&threads[i], NULL, pipeline_thread, &tasks[i]));
   }
   for (unsigned i = 0; i < 16; i++)
      pthread_join(threads[i], NULL);
   kk_metal_cache_destroy(cache);
   cache = create();
   for (unsigned i = 0; i < 16; i++) {
      tasks[i] = (struct pipeline_task){cache, i + 1, true, 100};
      assert(!pthread_create(&threads[i], NULL, pipeline_thread, &tasks[i]));
   }
   for (unsigned i = 0; i < 16; i++)
      pthread_join(threads[i], NULL);
   kk_metal_cache_destroy(cache);
   assert(atomic_load(&builds) == 16);
}

static void corruption_test(void)
{
   struct kk_metal_cache *cache = create();
   pipeline(cache, 1, false);
   pipeline(cache, 2, false);
   kk_metal_cache_destroy(cache);
   for (struct stored *entry = items; entry; entry = entry->next)
      if (entry->size == sizeof(struct payload))
         ((struct payload *)entry->data)->magic ^= 1;
   cache = create();
   pipeline(cache, 1, false);
   pipeline(cache, 2, false);
   kk_metal_cache_destroy(cache);
   cache = create();
   pipeline(cache, 1, true);
   pipeline(cache, 2, true);
   kk_metal_cache_destroy(cache);
}

static void disabled_test(void)
{
   assert(!kk_metal_cache_create(NULL, (void *)2, 0, (void *)3, 0));
   setenv("MESA_KK_PSO_CACHE_DISABLE", "1", 1);
   assert(!kk_metal_cache_create((void *)1, (void *)2, 0, (void *)3, 0));
}

static void seed_archives(unsigned count)
{
   for (unsigned id = 1; id <= count; id++) {
      struct payload payload = {.magic = captured.magic};
      payload.ids[id / 64] = 1ULL << (id % 64);
      cache_key blob, key, reference;
      disk_cache_compute_key((void *)1, &payload, sizeof(payload), blob);
      disk_cache_put((void *)1, blob, &payload, sizeof(payload), NULL);
      uint8_t input[256 + CACHE_KEY_SIZE] = {0};
      snprintf((char *)input, 256, "test-device-1");
      key_for_id(id, key);
      memcpy(input + 256, key, sizeof(key));
      disk_cache_compute_key((void *)1, input, sizeof(input), reference);
      disk_cache_put((void *)1, reference, blob, sizeof(blob), NULL);
   }
}

static void capacity_test(void)
{
   seed_archives(257);
   struct kk_metal_cache *cache = create();
   for (unsigned i = 1; i <= 256; i++)
      pipeline(cache, i, true);
   assert(atomic_load(&archive_loads) == 256);
   for (unsigned i = 1; i <= 256; i++)
      pipeline(cache, i, true);
   assert(atomic_load(&archive_loads) == 256);
   pipeline(cache, 1, true);
   pipeline(cache, 257, true);
   assert(atomic_load(&archive_loads) == 257);
   pipeline(cache, 1, true);
   assert(atomic_load(&archive_loads) == 257);
   pipeline(cache, 2, true);
   assert(atomic_load(&archive_loads) == 258);
   kk_metal_cache_destroy(cache);
   assert(pipeline_objects() == 0);
   assert(atomic_load(&builds) == 0);
}

static void import_test(void)
{
   seed_archives(256);
   struct kk_metal_cache *cache = create();
   struct pipeline_task tasks[16];
   pthread_t threads[16];
   for (unsigned i = 0; i < 16; i++) {
      tasks[i] = (struct pipeline_task){cache, 1, true, 100};
      assert(!pthread_create(&threads[i], NULL, pipeline_thread, &tasks[i]));
   }
   for (unsigned i = 0; i < 16; i++)
      pthread_join(threads[i], NULL);
   assert(pipeline_objects() == 1);
   for (unsigned i = 2; i <= 256; i++)
      pipeline(cache, i, true);
   unsigned loads = atomic_load(&archive_loads);
   for (unsigned i = 1; i <= 256; i++)
      pipeline(cache, i, true);
   assert(atomic_load(&archive_loads) == loads);
   kk_metal_cache_destroy(cache);
   assert(pipeline_objects() == 0);
}

static void eviction_test(void)
{
   seed_archives(257);
   struct kk_metal_cache *cache = create();
   held_archive_id = 1;
   struct pipeline_task task = {cache, 1, true, 1};
   pthread_t thread;
   assert(!pthread_create(&thread, NULL, pipeline_thread, &task));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&archive_entered, 5));
   pthread_mutex_unlock(&control_lock);
   for (unsigned i = 2; i <= 257; i++)
      pipeline(cache, i, true);
   assert(pipeline_objects() == 257);
   pthread_mutex_lock(&control_lock);
   allow_archive = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(thread, NULL);
   assert(pipeline_objects() == 256);
   kk_metal_cache_destroy(cache);
   assert(pipeline_objects() == 0);
}

static void idle_only_test(void)
{
   kk_metal_cache_flush(NULL);
   struct kk_metal_cache *cache = create();
   kk_metal_cache_flush(cache);
   assert(atomic_load(&snapshots) == 0);
   for (unsigned i = 1; i <= 257; i++)
      pipeline(cache, i, false);
   pthread_mutex_lock(&control_lock);
   assert(!wait_flag(&snapshot_entered, 2));
   pthread_mutex_unlock(&control_lock);
   assert(atomic_load(&snapshots) == 0);
   kk_metal_cache_flush(cache);
   assert(atomic_load(&snapshots) == 1);
   kk_metal_cache_flush(cache);
   assert(atomic_load(&snapshots) == 1);
   pipeline(cache, 258, false);
   kk_metal_cache_destroy(cache);
   assert(atomic_load(&snapshots) == 2);
   cache = create();
   for (unsigned i = 1; i <= 258; i++)
      pipeline(cache, i, true);
   kk_metal_cache_destroy(cache);
}

static void concurrent_idle_test(void)
{
   struct kk_metal_cache *cache = create();
   for (unsigned i = 1; i <= 32; i++)
      pipeline(cache, i, false);
   pthread_t threads[16];
   for (unsigned i = 0; i < 16; i++)
      assert(!pthread_create(&threads[i], NULL, flush_thread, cache));
   for (unsigned i = 0; i < 16; i++)
      pthread_join(threads[i], NULL);
   assert(atomic_load(&snapshots) == 1);
   kk_metal_cache_destroy(cache);
   cache = create();
   for (unsigned i = 1; i <= 32; i++)
      pipeline(cache, i, true);
   kk_metal_cache_destroy(cache);
}

static void wait_snapshots(unsigned count)
{
   struct timespec until;
   clock_gettime(CLOCK_REALTIME, &until);
   until.tv_sec += 5;
   pthread_mutex_lock(&control_lock);
   while (atomic_load(&snapshots) < count)
      assert(!pthread_cond_timedwait(&control_cond, &control_lock, &until));
   pthread_mutex_unlock(&control_lock);
}

static void automatic_test(bool retry)
{
   setenv("MESA_KK_PSO_CACHE_QUIET_MS", "10", 1);
   setenv("MESA_KK_PSO_CACHE_MAX_AGE_MS", "100", 1);
   struct kk_metal_cache *cache = create();
   fail_snapshot = retry;
   pipeline(cache, 1, false);
   wait_snapshots(retry ? 2 : 1);
   kk_metal_cache_flush(cache);
   pipeline(cache, 1, true);
   kk_metal_cache_destroy(cache);
   assert(atomic_load(&snapshots) == (retry ? 2 : 1));
}

static void age_test(bool age)
{
   setenv(age ? "MESA_KK_PSO_CACHE_MAX_AGE_MS" :
               "MESA_KK_PSO_CACHE_QUIET_MS", "100", 1);
   struct kk_metal_cache *cache = create();
   pipeline(cache, 1, false);
   blocked_id = 2;
   struct pipeline_task task = {cache, 2, false, 1};
   pthread_t thread;
   assert(!pthread_create(&thread, NULL, pipeline_thread, &task));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&compile_entered, 5));
   assert(!wait_flag(&snapshot_entered, 1));
   pthread_mutex_unlock(&control_lock);
   if (age) {
      pipeline(cache, 3, false);
      assert(atomic_load(&uncaptured_builds) == 1);
   }
   pthread_mutex_lock(&control_lock);
   allow_compile = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(thread, NULL);
   wait_snapshots(1);
   kk_metal_cache_flush(cache);
   kk_metal_cache_destroy(cache);
}

static void compile_failure_test(void)
{
   struct kk_metal_cache *cache = create();
   failed_compile_id = 5;
   cache_key key;
   key_for_id(5, key);
   bool hit = true;
   assert(!kk_metal_cache_new_render_pipeline(cache, key, (void *)5, &hit));
   assert(!hit);
   pipeline(cache, 1, false);
   hold_snapshot = true;
   pthread_t flusher;
   assert(!pthread_create(&flusher, NULL, flush_thread, cache));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&snapshot_entered, 5));
   pthread_mutex_unlock(&control_lock);
   failed_compile_id = 2;
   key_for_id(2, key);
   hit = true;
   assert(!kk_metal_cache_new_compute_pipeline(cache, key, (void *)2, 64, &hit));
   assert(!hit && atomic_load(&uncaptured_builds) == 1);
   pthread_mutex_lock(&control_lock);
   allow_snapshot = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(flusher, NULL);
   kk_metal_cache_destroy(cache);
}

static void degraded_test(bool disabled)
{
   setenv("MESA_KK_PSO_CACHE_QUIET_MS", "10", 1);
   setenv("MESA_KK_PSO_CACHE_MAX_AGE_MS", "10", 1);
   if (disabled)
      setenv("MESA_KK_PSO_CACHE_BYPASS_DISABLE", "1", 1);
   else
      fail_uncaptured = true;
   struct kk_metal_cache *cache = create();
   pipeline(cache, 1, false);
   pthread_mutex_lock(&control_lock);
   assert(!wait_flag(&snapshot_entered, 1));
   pthread_mutex_unlock(&control_lock);
   kk_metal_cache_flush(cache);
   assert(atomic_load(&snapshots) == 1);
   kk_metal_cache_destroy(cache);
}

static void *destroy_thread(void *arg)
{
   kk_metal_cache_destroy(arg);
   atomic_store(&destroy_finished, true);
   return NULL;
}

static void destroy_bypass_test(void)
{
   struct kk_metal_cache *cache = create();
   pipeline(cache, 1, false);
   hold_snapshot = true;
   pthread_t flusher;
   assert(!pthread_create(&flusher, NULL, flush_thread, cache));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&snapshot_entered, 5));
   blocked_id = 2;
   pthread_mutex_unlock(&control_lock);
   struct pipeline_task task = {cache, 2, false, 1};
   pthread_t producer, destroyer;
   assert(!pthread_create(&producer, NULL, pipeline_thread, &task));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&compile_entered, 5));
   allow_snapshot = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(flusher, NULL);
   assert(!pthread_create(&destroyer, NULL, destroy_thread, cache));
   struct timespec pause = {.tv_nsec = 100000000};
   nanosleep(&pause, NULL);
   assert(!atomic_load(&destroy_finished));
   assert(atomic_load(&uncaptured_objects) == 1);
   pthread_mutex_lock(&control_lock);
   allow_compile = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(producer, NULL);
   pthread_join(destroyer, NULL);
   assert(atomic_load(&destroy_finished));
}

static void destroy_flush_test(void)
{
   struct kk_metal_cache *cache = create();
   pipeline(cache, 1, false);
   hold_snapshot = true;
   pthread_t flusher, destroyer;
   assert(!pthread_create(&flusher, NULL, flush_thread, cache));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&snapshot_entered, 5));
   pthread_mutex_unlock(&control_lock);
   assert(!pthread_create(&destroyer, NULL, destroy_thread, cache));
   struct timespec pause = {.tv_nsec = 100000000};
   nanosleep(&pause, NULL);
   assert(!atomic_load(&destroy_finished));
   pthread_mutex_lock(&control_lock);
   allow_snapshot = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(flusher, NULL);
   pthread_join(destroyer, NULL);
   assert(atomic_load(&destroy_finished));
}

static void late_recapture_test(bool shutdown, unsigned id)
{
   struct kk_metal_cache *cache = create();
   pipeline(cache, 1, false);
   hold_snapshot = true;
   pthread_t flusher, producer, destroyer;
   assert(!pthread_create(&flusher, NULL, flush_thread, cache));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&snapshot_entered, 5));
   blocked_id = id;
   pthread_mutex_unlock(&control_lock);
   struct pipeline_task task = {cache, id, false, 1};
   assert(!pthread_create(&producer, NULL, pipeline_thread, &task));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&compile_entered, 5));
   allow_snapshot = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(flusher, NULL);
   if (shutdown) {
      assert(!pthread_create(&destroyer, NULL, destroy_thread, cache));
      struct timespec pause = {.tv_nsec = 100000000};
      nanosleep(&pause, NULL);
      assert(!atomic_load(&destroy_finished));
   }
   pthread_mutex_lock(&control_lock);
   allow_compile = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(producer, NULL);
   if (shutdown) {
      pthread_join(destroyer, NULL);
   } else {
      for (unsigned i = 0; atomic_load(&builds) < 3 && i < 500; i++) {
         struct timespec pause = {.tv_nsec = 10000000};
         nanosleep(&pause, NULL);
      }
      assert(atomic_load(&builds) == 3);
      kk_metal_cache_flush(cache);
      kk_metal_cache_destroy(cache);
   }
   assert(atomic_load(&uncaptured_builds) == 1);
   cache = create();
   pipeline(cache, id, true);
   kk_metal_cache_destroy(cache);
}

static void *exit_thread(void *arg)
{
   registered_exit();
   return NULL;
}

static void exit_race_test(void)
{
   struct kk_metal_cache *cache = create();
   pipeline(cache, 1, false);
   hold_disk = true;
   pthread_t exiter, destroyer;
   assert(registered_exit);
   assert(!pthread_create(&exiter, NULL, exit_thread, NULL));
   pthread_mutex_lock(&control_lock);
   assert(wait_flag(&disk_entered, 5));
   pthread_mutex_unlock(&control_lock);
   assert(!pthread_create(&destroyer, NULL, destroy_thread, cache));
   struct timespec pause = {.tv_nsec = 100000000};
   nanosleep(&pause, NULL);
   assert(!atomic_load(&destroy_finished));
   pthread_mutex_lock(&control_lock);
   allow_disk = true;
   pthread_cond_broadcast(&control_cond);
   pthread_mutex_unlock(&control_lock);
   pthread_join(exiter, NULL);
   pthread_join(destroyer, NULL);
   assert(atomic_load(&destroy_finished));
}

static void invalid_timing_test(void)
{
   const char *values[] = {"-1", "0", "garbage", "999999999999999999999"};
   for (unsigned i = 0; i < sizeof(values) / sizeof(values[0]); i++) {
      setenv("MESA_KK_PSO_CACHE_QUIET_MS", values[i], 1);
      setenv("MESA_KK_PSO_CACHE_MAX_AGE_MS", values[i], 1);
      struct kk_metal_cache *cache = create();
      pipeline(cache, 1 + i, false);
      pthread_mutex_lock(&control_lock);
      assert(!wait_flag(&snapshot_entered, 1));
      pthread_mutex_unlock(&control_lock);
      kk_metal_cache_destroy(cache);
      pthread_mutex_lock(&control_lock);
      snapshot_entered = false;
      pthread_mutex_unlock(&control_lock);
   }
}

int main(int argc, char **argv)
{
   assert(argc == 2);
   unsetenv("MESA_KK_PSO_CACHE_DISABLE");
   setenv("MESA_KK_PSO_CACHE_QUIET_MS", "2147483647", 1);
   setenv("MESA_KK_PSO_CACHE_MAX_AGE_MS", "2147483647", 1);
   setenv("MESA_KK_PSO_CACHE_MAX_PENDING", "2147483647", 1);
   if (!strcmp(argv[1], "late_recapture"))
      late_recapture_test(false, 2);
   else if (!strcmp(argv[1], "late_recapture_render"))
      late_recapture_test(false, 3);
   else if (!strcmp(argv[1], "shutdown_recapture"))
      late_recapture_test(true, 2);
   else if (!strcmp(argv[1], "shutdown_recapture_render"))
      late_recapture_test(true, 3);
   else if (!strcmp(argv[1], "race"))
      race_test(64);
   else if (!strcmp(argv[1], "idle_under_load"))
      race_test(256);
   else if (!strcmp(argv[1], "serialization"))
      serialization_test();
   else if (!strcmp(argv[1], "failure"))
      failure_test();
   else if (!strcmp(argv[1], "concurrency"))
      concurrency_test();
   else if (!strcmp(argv[1], "corruption"))
      corruption_test();
   else if (!strcmp(argv[1], "disabled"))
      disabled_test();
   else if (!strcmp(argv[1], "capacity"))
      capacity_test();
   else if (!strcmp(argv[1], "import"))
      import_test();
   else if (!strcmp(argv[1], "eviction"))
      eviction_test();
   else if (!strcmp(argv[1], "idle_only"))
      idle_only_test();
   else if (!strcmp(argv[1], "concurrent_idle"))
      concurrent_idle_test();
   else if (!strcmp(argv[1], "quiet"))
      automatic_test(false);
   else if (!strcmp(argv[1], "retry"))
      automatic_test(true);
   else if (!strcmp(argv[1], "age"))
      age_test(true);
   else if (!strcmp(argv[1], "quiet_active"))
      age_test(false);
   else if (!strcmp(argv[1], "compile_failure"))
      compile_failure_test();
   else if (!strcmp(argv[1], "uncaptured_failure"))
      degraded_test(false);
   else if (!strcmp(argv[1], "bypass_disabled"))
      degraded_test(true);
   else if (!strcmp(argv[1], "destroy_bypass"))
      destroy_bypass_test();
   else if (!strcmp(argv[1], "destroy_flush"))
      destroy_flush_test();
   else if (!strcmp(argv[1], "exit_race"))
      exit_race_test();
   else if (!strcmp(argv[1], "invalid_timing"))
      invalid_timing_test();
   else if (!strcmp(argv[1], "native_exit") || !strcmp(argv[1], "fast_exit") ||
            !strcmp(argv[1], "exit_registration_failure") ||
            !strcmp(argv[1], "exit_snapshot_failure")) {
      atomic_store(&fail_exit_registration, !strcmp(argv[1], "exit_registration_failure"));
      atomic_store(&fail_exit_snapshot, !strcmp(argv[1], "exit_snapshot_failure"));
      struct kk_metal_cache *cache = create();
      pipeline(cache, 1, false);
      if (!strcmp(argv[1], "fast_exit"))
         _exit(0);
      return 0;
   }
   else
      return 2;
   while (items) {
      struct stored *next = items->next;
      free(items->data);
      free(items);
      items = next;
   }
   assert(pipeline_objects() == 0);
   assert(atomic_load(&uncaptured_objects) == 0);
   printf("PASS %s\n", argv[1]);
   return 0;
}
