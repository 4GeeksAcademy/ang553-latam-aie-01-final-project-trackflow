# Caching and Performance Optimization Report — TrackFlow

## 1. Executive Summary

This optimization work focused on improving TrackFlow performance through measured and deliberate caching decisions rather than applying caching indiscriminately.

The work followed an evidence-first process:

1. The existing frontend and backend architecture was audited.
2. An HTTP timing middleware was added to FastAPI.
3. Candidate endpoints were evaluated based on cost, expected frequency, stability, privacy, and invalidation complexity.
4. A reproducible benchmark harness was created using isolated temporary databases.
5. Performance was measured with increasing dataset sizes.
6. Two backend read projections were selected for caching:
   - `GET /inventory/products`
   - `GET /inventory/orders`
7. A process-local TTL cache was implemented with explicit invalidation.
8. Security, TTL expiration, cache hits, cache misses, failed mutations, and freshness after invalidation were validated with automated tests.
9. The same benchmark methodology was repeated after caching.
10. On the frontend, `IncidentSummary` was moved behind a real dynamic loading boundary using `next/dynamic`.

Not every technically cacheable or memoizable component was optimized. Candidates that lacked sufficient evidence of meaningful performance benefit were intentionally left unchanged.

---

## 2. Measurement Methodology

### 2.1 API timing middleware

FastAPI requests are instrumented with a lightweight HTTP middleware using:

```python
time.perf_counter()

The dedicated logger is:

api.timing

Each request records:

METHOD PATH → STATUS | X.Xms

Example:

GET /inventory/products → 200 | 12.5ms

The middleware intentionally logs only:

HTTP method;
URL path;
response status;
total request duration.

It does not log:

bearer tokens;
Authorization headers;
cookies;
request bodies;
credentials;
query strings.

The timing wraps FastAPI's call_next(), so the measured duration includes the request processing pipeline such as authentication dependencies, database work, response-model validation, serialization, middleware, and ASGI processing.

It is therefore an application-level request latency metric, not an isolated SQL query timer.

2.2 Reproducible benchmark harness

A dedicated development benchmark was created at:

scripts/benchmark_cache_candidates.py

The benchmark does not use TrackFlow's persistent development or production data.

Every execution creates isolated temporary resources:

SQLite database for inventory;
TinyDB for authentication;
TinyDB for suppliers;
temporary benchmark user;
temporary JWT authentication.

All benchmark storage is created inside TemporaryDirectory and removed after execution.

No real credentials or tokens are printed.

2.3 Dataset profiles

Three deterministic profiles are available:

Profile	SKUs	Stock Entries	Stock Exits	Movements	Suppliers
Base	6	4	3	7	15
Medium	100	500	500	1,000	200
Large	500	2,500	2,500	5,000	1,000

Synthetic data is deterministic and varied across:

product names;
clients;
categories;
warehouses;
quantities;
references;
movement types;
tracking numbers;
supplier countries;
supplier categories.

These profiles are benchmark workloads only. They are not presented as production traffic estimates.

2.4 Request methodology

For each endpoint and profile:

1 warm-up request
+
10 measured requests

Requests are sequential and no concurrency is introduced.

The benchmark records:

api.timing;
client wall-clock time;
response body size;
minimum;
average;
median;
approximate p95;
maximum.

Because only ten measured samples are used, p95 is treated as an approximate diagnostic metric rather than a statistically representative production percentile.

3. Backend Candidates Evaluated
Resource	Evaluation	Decision	Reason
GET /inventory/orders	High cost with growing history	Cache	Highest measured cost and repeatable global projection
GET /inventory/products	Moderate cost, multiple frontend consumers	Cache	Good combination of cost and expected read frequency
GET /inventory/products/{id}	Low and stable cost	Do not cache	Approximately 2.5 ms even at large profile
GET /suppliers	Moderate cost	Not selected	Lower priority and additional cache-key/filter complexity
GET /auth/me	Personalized and sensitive	Do not cache globally	User-specific authentication data
GET /profiles/me	Personalized	Do not cache globally	Response belongs to current authenticated user
GET /users*	Sensitive administrative/user data	Do not cache globally	Privacy and authorization risk
GET /api/incidents/results/export	Depends on process-global _last_result	Do not cache	Existing result isolation model is unsuitable for shared caching

The final backend choices were therefore:

GET /inventory/products
GET /inventory/orders
4. Pre-Cache Performance

The small baseline was intentionally insufficient to justify caching by itself. With only six products and seven movements, most endpoints responded within a few milliseconds.

Increasing the workload revealed the scaling behavior.

4.1 Benchmark averages
Endpoint	Base avg API	Medium avg API	Large avg API	Large response
Products	2.73 ms	6.35 ms	12.51 ms	78,143 bytes
Product detail	2.51 ms	2.46 ms	2.57 ms	151 bytes
Orders	2.32 ms	21.84 ms	104.36 ms	1,440,824 bytes
Suppliers	1.72 ms	3.01 ms	10.60 ms	331,491 bytes
4.2 Interpretation
Orders

GET /inventory/orders showed the strongest degradation.

The endpoint:

reads all stock entries;
reads all stock exits;
bulk-loads referenced SKUs;
builds the complete movement projection in Python;
returns the full history without pagination.

Large-profile latency increased from:

2.32 ms → 104.36 ms

while the response reached approximately:

1.44 MB

This made orders the strongest caching candidate.

Products

GET /inventory/products performs three database queries:

all SKUs;
grouped inbound quantities;
grouped outbound quantities.

It does not contain an N+1 query pattern.

However, the endpoint calculates current_stock for the complete catalog and is consumed by multiple frontend flows.

Large-profile latency increased from:

2.73 ms → 12.51 ms

This combination of moderate cost and repeated reuse made it the second selected endpoint.

Product detail

GET /inventory/products/{id} remained effectively constant:

~2.5 ms

Its response is also very small.

Caching it would add invalidation complexity for very little demonstrated benefit, so it was intentionally excluded.

Suppliers

Suppliers uses TinyDB all() followed by filtering in Python.

Its cost increased with dataset size, but it was not selected because:

measured latency was lower than orders;
observed frontend reuse was lower than products;
country and category filters introduce multiple cache variants;
every supplier mutation could invalidate several variants.

It remains a reasonable future candidate if production evidence shows higher read frequency.

5. Backend Caching Strategy
5.1 Cache type

TrackFlow uses a process-local in-memory TTL cache.

The implementation is intentionally small and uses only the Python standard library.

Important characteristics:

thread-safe through RLock;
expiration based on time.monotonic();
lazy expiration;
explicit deletion/invalidation;
deterministic injectable clock for tests;
no cleanup background thread;
no Redis;
no new external dependency.

This design matches the current application architecture, where the observed development runtime uses a single Uvicorn process.

5.2 Products cache

Default TTL:

30 seconds

Configuration:

TRACKFLOW_PRODUCTS_CACHE_TTL_SECONDS

Cached representation:

list[SKUResponse]

The cached value is already detached from ORM session lifecycle concerns.

The cache key is process-local and distinguishes the logical resource and the database bind used by the active session.

Authentication still executes before the route handler reaches the cache.

5.3 Orders cache

Default TTL:

15 seconds

Configuration:

TRACKFLOW_ORDERS_CACHE_TTL_SECONDS

Cached representation:

list[InventoryOrderListItem]

The initial implementation briefly cached the raw service projection, which contained ORM SKU references.

That was corrected before final validation.

The final cache stores the prepared HTTP projection instead, avoiding persistence of ORM objects beyond their session lifecycle.

Authentication and FastAPI response processing still execute normally on cache hits.

6. Security and Privacy

Caching was implemented only after reviewing authentication and response scope.

During the performance audit, the inventory GET routes were found to be accessible without backend authentication even though:

the backoffice UI was protected by AuthGuard;
the frontend API client already sent bearer tokens;
inventory mutations required authentication;
orders expose user_uuid as an audit field.

The backend contract was hardened before caching.

The following inventory reads now require authentication:

GET /inventory/products
GET /inventory/products/{id}
GET /inventory/orders

Cache lookup occurs after FastAPI resolves:

Depends(get_current_user)

Therefore:

request
↓
authentication
↓
cache lookup
↓
HIT or MISS

A warm cache cannot bypass authentication.

The cache:

does not store bearer tokens;
does not include tokens in cache keys;
does not include user identity in cache keys;
is not segmented by user because these inventory projections currently return the same operational dataset to all authorized users.

Automated tests explicitly verify that unauthenticated requests continue returning 401 even after the corresponding cache has been warmed by an authenticated request.

user_uuid remains part of the authenticated orders projection because it is currently used by the backoffice as audit information.

User-specific endpoints such as /auth/me, /profiles/me, and /users* were not included in shared caching.

7. Invalidation and Freshness

Explicit invalidation is the primary consistency mechanism.

TTL is a secondary safety boundary.

7.1 Invalidation matrix
Mutation	Products cache	Orders cache
Create product	Invalidate	Preserve
Successful inbound movement	Invalidate	Invalidate
Successful outbound movement	Invalidate	Invalidate
Failed mutation	Preserve	Preserve
7.2 Why product creation preserves orders

Creating a new SKU changes the products catalog but does not create an inventory movement.

Therefore invalidating orders would discard a still-valid expensive projection without improving correctness.

7.3 Mutation ordering

Invalidation occurs only after successful persistence:

write
↓
commit
↓
refresh
↓
invalidate

If validation or persistence fails before this point, existing cache entries remain valid.

Automated tests explicitly validate that a failed outbound operation caused by insufficient stock does not discard warm products or orders caches.

8. Post-Cache Results

The same benchmark methodology was reused after caching.

8.1 Large profile
Endpoint	Pre-cache avg API	Cold MISS post-cache	Warm HIT avg API	Reduction	Approx. speedup	Payload
Products	12.51 ms	21.6 ms	1.84 ms	85.3%	6.8x	78,143 bytes
Orders	104.36 ms	82.7 ms	9.6 ms	90.8%	10.9x	1,440,824 bytes

The first request remains a cache MISS and still performs source work.

The primary benefit occurs on repeated reads within the TTL window.

8.2 Base profile
Endpoint	Pre-cache avg API	Cold MISS post-cache	Warm HIT avg API
Products	2.73 ms	13.5 ms	1.32 ms
Orders	2.32 ms	4.3 ms	1.39 ms

The base profile reinforces an important conclusion:

With tiny datasets, caching offers limited absolute savings.

The optimization becomes materially valuable as the underlying dataset grows.

8.3 Payload size

Caching does not reduce HTTP response size.

Large-profile responses remained approximately:

Products: 78 KB
Orders:   1.44 MB

This confirms that the improvement comes from avoiding repeated data-access and projection work, not from returning less data.

9. Freshness Verification

Caching was validated not only through timing measurements but also through real mutation flows.

9.1 Products

Observed sequence:

Stage	Cache state	Stock
First read	MISS	10
Second read	HIT	10
Inbound +7, then read	MISS	17
Next read	HIT	17

The stock calculation validates:

10 + 7 = 17

The post-mutation response is therefore fresh rather than stale cached data.

9.2 Orders

Observed sequence:

First read
→ MISS

Second read
→ HIT

Successful outbound
→ invalidate

Next read
→ MISS
→ new movement present

Following read
→ HIT
→ new movement still present

The validated lifecycle is:

write
→ invalidate
→ MISS
→ fresh data
→ HIT
10. Frontend Optimization
10.1 Lazy Loading implemented

The selected frontend candidate was:

IncidentSummary

within:

/backoffice/incidents

It was selected because it represents a real additional loading boundary inside an existing route:

/incidents
↓
upload UI available immediately
↓
user analyzes CSV
↓
result becomes available
↓
IncidentSummary is needed

The component is now loaded using:

next/dynamic

No ssr: false override was added because the component itself does not require browser-only APIs.

10.2 Preserving the original empty state

Before optimization, IncidentSummary was mounted even when no analysis result existed and displayed a substantial empty-state interface.

Removing the component entirely from the initial render would therefore have changed the user experience.

To preserve behavior without loading the full summary component, the empty state was extracted into:

IncidentSummaryPlaceholder

The route loads this lightweight placeholder statically.

The full IncidentSummary module is requested dynamically only once a real analysis result exists.

The same placeholder is reused inside IncidentSummary for its null state, preventing duplicated UI markup.

The production build confirmed a separate dynamic-loading boundary for IncidentSummary.

10.3 Why a second Lazy Loading boundary was not added

Several additional candidates were audited, including:

website ContactSection;
website CoverageSection;
supplier UI;
product list;
order history;
inventory forms;
application form;
operational dashboard.

A second optimization was deliberately not implemented.

Reasons included:

ContactSection and CoverageSection are static Server Components and do not contribute meaningful client JavaScript of their own;
many backoffice components are the primary content of their route and delaying them would worsen the first interaction;
route-level code splitting is already provided by Next.js App Router;
splitting internal sections that are always needed at the same time would create artificial boundaries without demonstrated performance value.

The optimization therefore prioritizes one real boundary over multiple cosmetic ones.

11. useMemo Evaluation

useMemo was investigated but intentionally not implemented.

11.1 IncidentSummary

IncidentSummary performs derived transformations such as:

category breakdown;
status breakdown;
country breakdown;
invalid-record ordering;
score distribution.

However, these collections are bounded by domain values rather than by the total number of uploaded incidents.

For example:

country breakdown has a very small fixed domain;
score distribution contains five scores;
categories and statuses are limited sets;
invalid breakdown is bounded by known validation error codes.

A CSV containing thousands of records therefore does not result in thousands of frontend items being sorted or transformed.

The expensive CSV analysis occurs primarily in the backend.

11.2 OperationalSnapshot

buildOperationalSnapshot() performs more substantial operations, including:

filtering;
sorting;
counting;
validation;
lookup;
aggregation.

However, in the current application it operates on:

3 sample products
3 sample carriers
1 sample shipment

Memoizing that workload would provide negligible benefit.

11.3 Inventory forms and lists

The remaining candidates largely perform direct JSX rendering through .map().

No significant render-time:

sorting;
grouping;
normalization;
expensive reduction;

was found.

Decision

No useMemo was introduced because the measured/current workload did not justify the additional complexity.

This follows the optimization principle used throughout the ticket:

measure first
→ optimize demonstrated cost
→ avoid premature memoization
12. Freshness vs. Performance Trade-off

Caching necessarily introduces the possibility of stale data.

TrackFlow addresses this through explicit invalidation plus short TTLs.

Products

Products include calculated current_stock.

Any inbound or outbound movement may therefore change the response.

Strategy:

TTL: 30 seconds
+
immediate invalidation after inventory mutations

The TTL is not the main consistency mechanism.

It acts as a maximum lifetime for an entry if an invalidation is accidentally missed or if data changes outside the known mutation paths.

Orders

The complete movement history changes whenever inbound or outbound movements are recorded.

Because the resource is highly mutable and has a large payload, a shorter TTL is used:

TTL: 15 seconds

Again, successful mutations invalidate the cache immediately.

The shorter TTL reduces the maximum stale-data window if an invalidation is missed.

This design improves repeated-read performance while avoiding long-lived stale operational data.

It does not provide distributed strong consistency.

13. Limitations
Process-local cache

The cache exists inside the Python process.

Multiple:

Uvicorn workers;
processes;
containers;
replicas;

would maintain independent caches.

Invalidation in one process would not automatically invalidate another.

A future distributed deployment could use Redis or another shared cache/invalidation mechanism, but adding distributed infrastructure was outside the scope of this optimization.

Synthetic benchmark

Benchmark data is deterministic and realistic enough to exercise joins, aggregation, serialization, and filtering, but it is still synthetic.

The measurements were performed with:

temporary SQLite;
temporary TinyDB;
ASGITransport;
sequential requests;
no representative concurrency.

The results demonstrate relative behavior in the development environment, not production SLOs.

Orders payload

The large-profile orders response remains approximately:

1.44 MB

Caching avoids repeated query and projection work, but it does not solve the size of the response.

Pagination would address a different problem:

Caching
→ avoids repeating expensive work.

Pagination
→ reduces the amount of data queried, serialized and transferred.

Pagination was intentionally not introduced as part of this caching ticket.

Serialization cost

The cache preserves FastAPI's normal response_model pipeline.

This keeps HTTP contracts safe and simple but means large responses are still validated and serialized on every request.

For orders, this explains why a warm hit is much faster than the original request but not effectively zero-cost.

14. Verification
Backend

Final complete Python test suite:

193 passed

Coverage includes:

TTL primitive MISS/HIT;
expiration;
entry replacement;
invalid TTL;
products cache MISS/HIT;
products 30-second expiration;
orders cache MISS/HIT;
orders 15-second expiration;
product creation invalidation;
inbound invalidation;
outbound invalidation;
preservation after failed mutation;
authentication with warm cache;
critical response fields including user_uuid.
Frontend

Modified incident files pass targeted ESLint validation.

The backoffice production build also passes:

Next.js production build: PASS
/incidents generation: PASS

The global backoffice lint command is not claimed to pass because unrelated pre-existing lint issues exist elsewhere in the application.

The changed incident files themselves pass ESLint without errors or warnings.

15. Relevant Files
Backend caching
services/api/cache.py
services/api/routes/inventory.py
services/api/inventory_service.py
Timing and benchmark
services/api/main.py
scripts/benchmark_cache_candidates.py
tests/test_api_timing.py
Cache tests
tests/test_cache.py
tests/test_inventory_products.py
tests/test_inventory_orders.py
tests/test_inventory_auth.py
Frontend
uis/backoffice/app/incidents/page.tsx
uis/backoffice/components/incidents/IncidentSummary.tsx
uis/backoffice/components/incidents/IncidentSummaryPlaceholder.tsx
16. Final Decision Summary
Implemented
FastAPI request timing middleware.
Reproducible isolated performance benchmark.
Process-local thread-safe TTL cache.
GET /inventory/products caching.
GET /inventory/orders caching.
Explicit mutation-driven invalidation.
Auth-before-cache enforcement.
30-second products TTL.
15-second orders TTL.
Lazy Loading of IncidentSummary.
Shared lightweight initial placeholder.
Evaluated but intentionally not implemented
Redis.
Distributed caching.
GET /inventory/products/{id} caching.
Suppliers caching.
Auth/profile/users shared caching.
Incidents export caching.
Second artificial Lazy Loading boundary.
useMemo without demonstrated expensive render calculations.
Orders pagination.

The final implementation favors measurable benefit, explicit consistency rules, and maintainable complexity over maximizing the number of optimizations applied.