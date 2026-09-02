# Third-party notices

This repository contains original Hermes adapter/sidecar code and source
patches applied to two independently maintained projects during the local
build. No prebuilt RustPush binary is distributed here.

## OpenBubbles/rustpush

- Source: https://github.com/OpenBubbles/rustpush
- Pinned revision: `7725a323e096bd05ec8446dd50c2f7786956340e`
- License: Server Side Public License, v1
- License text: `LICENSES/SSPL-1.0.txt`
- Upstream exception notice: `LICENSES/rustpush-exceptions.txt`
- Local modifications: `patches/rustpush-secure-state.patch`

The exception notice names OpenBubbles; it is reproduced verbatim and should
not be read as granting an exception to this project.

## lrhodin/imessage (Corten wrapper)

- Source: https://github.com/lrhodin/imessage
- Pinned revision: `a8514467590026aa5f27865a4b98686c656f2d6f`
- License: Mozilla Public License 2.0
- License text: `LICENSES/MPL-2.0.txt`
- Local modifications: `patches/corten-local.patch`

## Repository license boundaries

Original files in this repository are available under the MIT License in
`LICENSE`. The two patch files modify their respective upstream projects and
remain subject to the upstream licenses listed above. A locally compiled
runtime includes upstream code, so review and comply with those licenses before
redistribution or network-service deployment.
