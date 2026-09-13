# Frontend Performance Audit Report

## Executive summary

TrackFlow's Website and Backoffice were audited with Lighthouse 13.4.1 before and after the frontend audit work. The official comparison uses the same development/Codespaces setup as the baseline so that the BEFORE and AFTER runs remain methodologically comparable.

The audit produced two confirmed Lighthouse score improvements:

- Website `/application` Accessibility improved from **98 to 100** on both Desktop and Mobile.
- Backoffice `/` Accessibility improved from **95 to 100** on both Desktop and Mobile.

Best Practices remained at **100** in all final runs. SEO remained at **60** for reasons that were investigated separately: the public Website is correctly configured as indexable in application code, but the Codespaces forwarded proxy injects an `x-robots-tag: noindex, nofollow` response directive; the Backoffice intentionally declares `noindex, nofollow` because it is an internal application.

Performance scores were more variable in the final development-mode runs. The report therefore does **not** claim a reproducible Performance-score improvement where the evidence does not support one. The most important performance finding remains the Backoffice mobile render/authentication path and the substantial Next.js development-tooling overhead observed in the lab environment.

## Scope and methodology

Official Lighthouse coverage:

| Frontend | Route | Desktop | Mobile |
|---|---|---:|---:|
| Website | `/` | Yes | Yes |
| Website | `/application` | Yes | Yes |
| Backoffice | `/` | Yes | Yes |

Lighthouse categories reviewed:

- Performance
- Accessibility
- Best Practices
- SEO

Key metrics reviewed:

- TTFB
- LCP
- CLS
- INP
- FCP
- TBT
- Speed Index

The audit followed the project's installed `performance` and `core-web-vitals` skills. Measurement was performed before optimization, source and trace evidence were used for attribution, and TBT was treated only as a lab responsiveness diagnostic rather than as a replacement for INP.

The official BEFORE/AFTER comparison remains on the same `next dev` + Codespaces-forwarded environment. A production build was inspected during diagnosis, but production-mode results were not substituted for the official baseline because that would invalidate direct comparison.

## Lighthouse score comparison

| Route | Device | Performance | Accessibility | Best Practices | SEO |
|---|---|---:|---:|---:|---:|
| Website `/` | Desktop | **100 → 99** | 100 → 100 | 100 → 100 | 60 → 60 |
| Website `/` | Mobile | **89 → 76** | 100 → 100 | 100 → 100 | 60 → 60 |
| Website `/application` | Desktop | 100 → 100 | **98 → 100** | 100 → 100 | 60 → 60 |
| Website `/application` | Mobile | **95 → 84** | **98 → 100** | 100 → 100 | 60 → 60 |
| Backoffice `/` | Desktop | **96 → 80** | **95 → 100** | 100 → 100 | 60 → 60 |
| Backoffice `/` | Mobile | **65 → 55** | **95 → 100** | 100 → 100 | 60 → 60 |

### Confirmed score improvements

The ticket requires a measurable Lighthouse score improvement for each frontend. That requirement is satisfied through Accessibility:

- Website `/application`: **98 → 100**
- Backoffice `/`: **95 → 100**

These changes are deterministic and directly tied to targeted accessibility fixes rather than Lighthouse-specific workarounds.

## Key metric comparison

### Website `/`

| Metric | Desktop BEFORE | Desktop AFTER | Mobile BEFORE | Mobile AFTER |
|---|---:|---:|---:|---:|
| FCP | 0.5 s | **0.3 s** | 1.9 s | **1.0 s** |
| LCP | 0.5 s | **0.3 s** | 2.1 s | **1.2 s** |
| TBT | 10 ms | 112 ms | 330 ms | 1245 ms |
| CLS | 0 | 0 | 0 | 0 |
| Speed Index | 0.7 s | **0.5 s** | 3.4 s | **1.1 s** |

The Website home has no application-owned Client Components, no data fetch, no third-party scripts, no images, and no remote fonts in its initial route. The mobile AFTER run illustrates why a single Lighthouse Performance score should not be over-interpreted in this environment: FCP, LCP, and Speed Index improved substantially while TBT increased to 1245 ms, reducing the aggregate Performance score.

### Website `/application`

| Metric | Desktop BEFORE | Desktop AFTER | Mobile BEFORE | Mobile AFTER |
|---|---:|---:|---:|---:|
| FCP | 0.4 s | **0.3 s** | 1.0 s | 1.4 s |
| LCP | 0.4 s | 0.6 s | 1.0 s | 1.8 s |
| TBT | 10 ms | 70 ms | 250 ms | 618 ms |
| CLS | 0 | 0 | 0 | 0 |
| Speed Index | 0.6 s | 0.7 s | 1.0 s | 1.9 s |

The accessibility defect on this route was fixed by correcting the footer heading hierarchy. The change moved Accessibility from 98 to 100 without changing layout, behavior, or application logic.

### Backoffice `/`

| Metric | Desktop BEFORE | Desktop AFTER | Mobile BEFORE | Mobile AFTER |
|---|---:|---:|---:|---:|
| FCP | 0.4 s | 0.4 s | 1.0 s | 1.7 s |
| LCP | 1.3 s | 1.5 s | 5.9 s | 6.5 s |
| TBT | 20 ms | 190 ms | 510 ms | 842 ms |
| CLS | 0 | 0 | 0 | 0 |
| Speed Index | 1.2 s | 4.2 s | 2.5 s | 3.5 s |

Backoffice Mobile remains the largest performance concern. Diagnosis showed that the dashboard content is protected by `AuthGuard`; with an existing token, the protected content does not render until `/auth/me` validation finishes. Development-mode framework/tooling work also contributes substantial main-thread cost. A small client-boundary optimization experiment was tested and then reverted because it did not produce a reproducible improvement.

The authentication architecture was not weakened merely to improve a Lighthouse score. Rendering protected content early, trusting local storage without server validation, removing `/auth/me`, or restructuring authentication would change security/behavior semantics and was outside the scope of this targeted audit.

## Final AFTER metrics and TTFB

The final JSON reports contain the following lab values:

| Route | Device | TTFB | FCP | LCP | TBT | CLS | Speed Index |
|---|---|---:|---:|---:|---:|---:|---:|
| Website `/` | Desktop | 244 ms | 323 ms | 345 ms | 112 ms | 0 | 517 ms |
| Website `/` | Mobile | 711 ms | 1045 ms | 1200 ms | 1245 ms | 0 | 1132 ms |
| Website `/application` | Desktop | 251 ms | 331 ms | 571 ms | 70 ms | 0 | 661 ms |
| Website `/application` | Mobile | 1140 ms | 1447 ms | 1784 ms | 618 ms | 0 | 1887 ms |
| Backoffice `/` | Desktop | 353 ms | 433 ms | 1508 ms | 190 ms | 0 | 4231 ms |
| Backoffice `/` | Mobile | 1234 ms | 1735 ms | 6531 ms | 842 ms | 0 | 3471 ms |

TTFB was reviewed as part of the audit, but no TTFB improvement claim is made. Codespaces forwarding, development-mode execution, authentication traffic, and run-to-run environmental variability materially influence this metric.

## INP review

INP was reviewed separately with Chrome DevTools Live Metrics because normal Lighthouse Navigation runs do not provide a representative INP measurement without real user interaction.

Current local diagnostics:

- Website `/application`: **104 ms — Good**
- Website `/`: approximately **110 ms — Good**
- Backoffice `/`: the dashboard exposes too little meaningful interaction to claim a representative local INP sample.

No BEFORE/AFTER INP delta is reported because INP was not captured during the original baseline.

**TBT is not treated as INP.** TBT is included only as a laboratory diagnostic for main-thread blocking.

## Accessibility corrections

### Website `/application`

The footer heading hierarchy caused Lighthouse's `heading-order` audit to fail.

Correction:

- Changed the footer `TrackFlow` heading from `h3` to `h2`.
- No style, layout, or behavior change.

Result:

- Desktop Accessibility: **98 → 100**
- Mobile Accessibility: **98 → 100**

### Backoffice `/`

The initial Accessibility score of 95 was caused by contrast failures in the Backoffice header. A first text-color-only attempt was insufficient on Mobile, so the final targeted correction also removed translucent backgrounds and fixed the small-screen header layout.

Final changes included:

- Opaque `bg-slate-950` header background.
- Higher-contrast navigation text.
- Opaque `bg-rose-950` Logout button with white text.
- Responsive `flex-col` / `md:flex-row` layout.
- Full-width, wrapping mobile navigation.

Result:

- Desktop Accessibility: **95 → 100**
- Mobile Accessibility: **95 → 100**

## SEO result and attribution

All six BEFORE runs and all six AFTER runs report SEO = 60 because `is-crawlable` is the failing SEO audit.

### Website

The Website application code explicitly declares:

- `index: true`
- `follow: true`

Localhost inspection confirmed:

- `<meta name="robots" content="index, follow">`
- No application-generated `x-robots-tag: noindex, nofollow`

The Codespaces-forwarded Lighthouse requests report:

- `x-robots-tag: noindex, nofollow`

No Website middleware, route handler, `next.config` header, or `vercel.json` configuration was found that generates this directive.

Conclusion: Website SEO 60 is an **audit-environment artifact caused by the Codespaces forwarded proxy**, not a defect in Website SEO metadata. No incorrect application workaround was added merely to increase Lighthouse.

### Backoffice

Backoffice explicitly declares:

- `index: false`
- `follow: false`

This is intentional because Backoffice is an internal application. It should not be made indexable merely to increase Lighthouse SEO.

## Performance findings

### Website home

Confirmed characteristics:

- Static prerendered route.
- Server Components throughout the home route.
- No application-owned Client Components in `/`.
- No `useEffect` / `useState` hydration path in the home tree.
- No API fetch during initial render.
- No third-party scripts.
- No images.
- No remote fonts.
- No application-owned layout-measurement code attributable to the reported forced reflow.

The baseline showed significant work in Next.js development tooling, including Next DevTools. Because no application-owned performance change was sufficiently supported by evidence, the Website was not modified speculatively with `dynamic`, `memo`, image changes, or other unjustified optimizations.

### Backoffice mobile

Confirmed findings:

- The dashboard LCP element is static text in `OperationalSummary`.
- `OperationalSummary` itself does not fetch data.
- Protected content is gated by `AuthGuard`.
- `AuthProvider` validates an existing token through `/auth/me` before authentication is marked complete.
- The dashboard therefore cannot render through the guard until authentication validation completes.
- Development-mode framework/tooling JavaScript contributes substantial CPU/main-thread work.
- CORS preflight is also present in the forwarded environment.

A small experiment moved the header's logout interaction into a narrower client boundary. Three follow-up Mobile runs failed to show a reproducible improvement, so the experiment was reverted. This prevented an unsupported optimization from remaining in the codebase.

## Reusable refactor

The audit identified multiple duplication candidates. The required reusable refactor was implemented using:

`uis/backoffice/hooks/useApiResource.ts`

`useApiResource<T>` now centralizes the duplicated initial-resource lifecycle used by Products and Orders:

- `data`
- `isLoading`
- `error`
- async loader execution
- fallback errors
- cancellation on unmount
- reload behavior

Integrated routes:

- Backoffice Inventory Products
- Backoffice Inventory Orders

This refactor is a maintainability improvement and is **not claimed as a Lighthouse performance optimization**.

Other duplication candidates documented in `AUDIT.md` were intentionally left untouched to keep scope targeted.

## Highest-impact confirmed change

The highest-impact **confirmed code change** in this ticket was the Accessibility remediation.

Measured impact:

- Website `/application`: Accessibility **98 → 100**
- Backoffice `/`: Accessibility **95 → 100**
- Best Practices remained 100.
- CLS remained 0.
- No security or authentication semantics were weakened.

For performance, the highest-impact result was the diagnosis itself: the Backoffice Mobile bottleneck was narrowed to the protected render/authentication path plus substantial development-tooling overhead, while unsupported changes were avoided or reverted.

## Limitations

1. Official Lighthouse runs use `next dev` in Codespaces because the BEFORE baseline was created under those conditions.
2. Next.js development tooling introduces JavaScript and CPU overhead not representative of a production build.
3. Codespaces forwarding affects network timing and injects a crawler-blocking header into the Website response.
4. Lighthouse lab runs are variable, especially for TBT and aggregate Performance score.
5. INP was not captured in the original baseline and therefore has no valid BEFORE/AFTER delta.
6. TBT is not substituted for INP.
7. A production/RUM follow-up would provide stronger real-user Core Web Vitals evidence, but was outside this ticket's required scope.

## Evidence files

### BEFORE

Official baseline evidence is stored under:

`audit/before/`

Including the six Lighthouse JSON reports and the corresponding score/metric screenshots.

### AFTER

Final evidence should be stored under:

`audit/after/`

Expected JSON names:

- `website-home-desktop.json`
- `website-home-mobile.json`
- `website-application-desktop.json`
- `website-application-mobile.json`
- `backoffice-dashboard-desktop.json`
- `backoffice-dashboard-mobile.json`

Corresponding score and metrics screenshots use the same basename plus `.png` and `-metrics.png`.

## Conclusion

The frontend audit completed the required before/after Lighthouse process, documented concrete root causes, applied targeted fixes, integrated a reusable Custom Hook, and avoided speculative or security-changing performance modifications.

The strongest reproducible improvement was Accessibility: both frontends now reach 100 in the corrected routes. Performance measurements exposed meaningful development-environment variability and a persistent Backoffice Mobile bottleneck, but the audit does not overstate unproven performance gains. SEO 60 is explained by environment behavior on the public Website and intentional indexing policy on the internal Backoffice.

The resulting implementation and report prioritize measurable evidence, semantic correctness, and maintainability over artificially maximizing Lighthouse scores.
