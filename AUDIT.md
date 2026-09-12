# Frontend Performance Audit

## Scope

This baseline audit covers the TrackFlow website and backoffice frontends. Lighthouse measurements were collected before any frontend performance optimizations. The same six-measurement matrix will be reused for the AFTER comparison.

The measurements were executed in the Codespaces environment using Lighthouse Navigation mode. Desktop and Mobile form factors were measured according to the official matrix below. The complete Lighthouse JSON exports are the source of truth for scores and metrics.

## Audit Matrix

| Frontend | Route | Device |
|---|---|---|
| Website | `/` | Desktop |
| Website | `/` | Mobile |
| Website | `/application` | Desktop |
| Website | `/application` | Mobile |
| Backoffice | `/` | Desktop |
| Backoffice | `/` | Mobile |

## Initial Lighthouse Scores

| Frontend | Route | Device | Performance | Accessibility | Best Practices | SEO |
|---|---|---|---:|---:|---:|---:|
| Website | `/` | Desktop | 100 | 100 | 100 | 60 |
| Website | `/` | Mobile | 89 | 100 | 100 | 60 |
| Website | `/application` | Desktop | 100 | 98 | 100 | 60 |
| Website | `/application` | Mobile | 95 | 98 | 100 | 60 |
| Backoffice | `/` | Desktop | 96 | 95 | 100 | 60 |
| Backoffice | `/` | Mobile | 65 | 95 | 100 | 60 |

## Initial Performance Metrics

| Frontend | Route | Device | FCP | LCP | TBT | CLS | Speed Index |
|---|---|---|---:|---:|---:|---:|---:|
| Website | `/` | Desktop | 0.5 s | 0.5 s | 10 ms | 0 | 0.7 s |
| Website | `/` | Mobile | 1.9 s | 2.1 s | 330 ms | 0 | 3.4 s |
| Website | `/application` | Desktop | 0.4 s | 0.4 s | 10 ms | 0 | 0.6 s |
| Website | `/application` | Mobile | 1.0 s | 1.0 s | 250 ms | 0 | 1.0 s |
| Backoffice | `/` | Desktop | 0.4 s | 1.3 s | 20 ms | 0 | 1.2 s |
| Backoffice | `/` | Mobile | 1.0 s | 5.9 s | 510 ms | 0 | 2.5 s |

## Initial Observations

- Website Desktop presents very high performance, with a Performance score of 100.
- Website Mobile is lower than Website Desktop, with a Performance score of 89.
- Website `/application` Mobile maintains Performance above 90, at 95.
- Backoffice Desktop presents a Performance score of 96.
- Backoffice Mobile is the critical case in the measured matrix, with a Performance score of 65.
- Backoffice Mobile LCP is 5.9 s.
- Backoffice Mobile TBT is 510 ms.
- Website Home Mobile TBT is 330 ms.
- CLS is 0 in all six measurements.
- SEO is 60 in all six measurements.
- Accessibility is 95 for Backoffice and 98 for `/application`.

These observations describe the measured baseline only. They do not assign root causes or propose implementation changes.

## Evidence

The baseline evidence is stored in [`audit/before/`](audit/before/).

- PNG files are the visual evidence captured during the Lighthouse measurements.
- JSON files are the complete Lighthouse exports used for validation.
- The JSON exports are the source of truth for scores, form factor, URLs, and performance metrics.
- A separate Website Home mobile score screenshot was not present in the original evidence; only the existing score screenshot was retained and normalized as `website-home-desktop.png`.

## Baseline Priorities

These are investigation priorities based on the baseline evidence, not implementation proposals:

1. Backoffice Mobile — LCP
2. Backoffice Mobile — TBT
3. Website Home Mobile — TBT / Speed Index
4. SEO — score 60
5. Accessibility — scores 95/98
