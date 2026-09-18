# Third-party notices

Asha is free and open-source software under the MIT licence (see [`LICENSE`](LICENSE)).
That MIT licence covers **Asha's own code only**. Asha is built and shipped with
the third-party components listed below; each component remains under its own
licence, and the copyright and licence terms of those projects apply to their
respective parts — the MIT licence does not relicense them. This list is
generated from what the packaging scripts (`prototype/packaging/build_macos.sh`,
`build_windows.ps1`) and the pinned `prototype/requirements.txt` actually vendor
into the app bundle.

> This file is informational and does not modify any third-party licence. Full
> licence texts for the bundled components ship inside the built app under
> `Contents/Resources/` (macOS) / `Resources\` (Windows) next to each component
> and in the Python runtime's `.dist-info` directories. For the copyleft
> components, the corresponding source can be obtained at the URLs below.

## Bundled runtimes, gateway and models

| Component | Version | Licence | Source |
|---|---|---|---|
| CPython (relocatable build) | 3.12.14 (build tag 20260901) | PSF-2.0 | `https://github.com/astral-sh/python-build-standalone` — `cpython-3.12.14+20260901-<arch>-install_only.tar.gz` |
| Node.js runtime | v24.21.0 LTS | MIT (Node.js also bundles libraries under their own licences; see its `LICENSE`) | `https://nodejs.org/dist/v24.21.0/` |
| OmniRoute | 3.8.50 | MIT | npm: `https://registry.npmjs.org/omniroute/-/omniroute-3.8.50.tgz`; project: `https://github.com/diegosouzapw/OmniRoute` |
| OmniRoute production dependencies (`vendor/omniroute/node_modules/`) | resolved at build time | each package's own licence (mostly MIT/ISC/Apache-2.0) | npm transitive dependencies; per-package licence files are kept in place |
| Kokoro model — `kokoro-v1.0.onnx`, `voices-v1.0.bin` | v1.0 | Apache-2.0 (Kokoro-82M, `hexgrad/Kokoro-82M`); the `kokoro-onnx` packaging is MIT | `https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0` |
| Moonshine voice model (English) | models fetched by `moonshine-voice==0.1.5` | MIT (moonshine-voice package, `usefulsensors/moonshine`) | `https://github.com/usefulsensors/moonshine`; package `moonshine-voice==0.1.5` |

## Python packages pinned in `prototype/requirements.txt`

| Package | Version | Licence | Source |
|---|---|---|---|
| pipecat-ai | 1.8.1 | BSD-2-Clause | PyPI |
| openai | 2.54.0 | Apache-2.0 | PyPI |
| httpx | 0.28.1 | BSD-3-Clause | PyPI |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | PyPI |
| fastapi | 0.141.1 | MIT | PyPI |
| uvicorn | 0.52.4 | BSD-3-Clause | PyPI |
| websockets | 17.1 | BSD-3-Clause | PyPI |
| python-dotenv | 1.2.3 | BSD-3-Clause | PyPI |
| pdfplumber | 0.11.10 | MIT | PyPI |
| kokoro-onnx | 0.6.1 | MIT | PyPI |
| moonshine-voice | 0.1.5 | MIT | PyPI |

## Transitive Python packages in the bundled runtime

These are installed into the bundled CPython runtime when the pinned
requirements are resolved. Licences below are taken from each package's own
metadata (PyPI project / package metadata) at the pinned version.

| Package | Version | Licence |
|---|---|---|
| aiofiles | 25.1.0 | Apache-2.0 |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 |
| aiosignal | 1.4.0 | Apache-2.0 |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.1 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cloudpickle | 3.1.2 | BSD-3-Clause |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| defusedxml | 0.7.1 | PSF-2.0 |
| distro | 1.9.0 | Apache-2.0 |
| dlinfo | 2.0.0 | MIT |
| docopt | 0.6.2 | MIT |
| docstring_parser | 0.18.0 | MIT |
| espeakng-loader | 0.2.4 | see note — bundles eSpeak NG (GPL-3.0-or-later) |
| filelock | 3.32.7 | MIT |
| flatbuffers | 25.12.19 | Apache-2.0 |
| frozenlist | 1.8.0 | Apache-2.0 |
| google-crc32c | 1.8.0 | Apache-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| idna | 3.19 | BSD-3-Clause |
| jiter | 0.17.0 | MIT |
| joblib | 1.6.0 | BSD-3-Clause |
| llvmlite | 0.49.0 | BSD-2-Clause AND Apache-2.0 WITH LLVM-exception |
| loguru | 0.7.3 | MIT |
| loudness | 0.2.0 | MIT |
| Markdown | 3.10.3 | BSD-3-Clause |
| mpmath | 1.3.0 | BSD |
| multidict | 6.8.0 | Apache-2.0 |
| nltk | 3.10.3 | Apache-2.0 |
| num2words | 0.5.14 | LGPL-2.1-or-later |
| numba | 0.67.0 | BSD-2-Clause |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime | 1.24.4 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pdfminer.six | 20260107 | MIT |
| phonemizer | 3.4.0 | GPL-3.0-or-later |
| pillow | 12.3.0 | MIT-CMU |
| pip | 26.2.1 | MIT |
| platformdirs | 4.11.8 | MIT |
| propcache | 0.5.4 | Apache-2.0 |
| protobuf | 6.33.6 | BSD-3-Clause |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic_core | 2.46.5 | MIT |
| pypdfium2 | 5.13.0 | BSD-3-Clause AND Apache-2.0 (plus dependency licences) |
| PyYAML | 6.0.3 | MIT |
| regex | 2026.9.10 | Apache-2.0 AND CNRI-Python |
| requests | 2.34.2 | Apache-2.0 |
| resampy | 0.4.3 | ISC |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| sounddevice | 0.5.6 | MIT |
| soxr | 1.0.0 | LGPL-2.1-or-later |
| starlette | 1.6.0 | BSD-3-Clause |
| sympy | 1.14.0 | BSD |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| urllib3 | 2.8.0 | MIT |
| yarl | 1.25.1 | Apache-2.0 |

## Copyleft components — shipped with notices and source links

The Asha desktop bundle deliberately includes the following copyleft components
as part of its on-device Kokoro / `kokoro-onnx` text-to-speech stack. They are
distributed under their own licences, with their notices and corresponding
source links given here. The repository's MIT licence does not apply to these
components and does not relicense them.

| Component | Version | Licence (SPDX) | Source for this version |
|---|---|---|---|
| phonemizer | 3.4.0 | GPL-3.0-or-later | `https://github.com/bootphon/phonemizer` (PyPI: `phonemizer==3.4.0`) |
| espeakng-loader (bundles eSpeak NG) | 0.2.4 | the loader declares no licence in its PyPI metadata; the eSpeak NG library and data it bundles are GPL-3.0-or-later | loader: `https://pypi.org/project/espeakng-loader/0.2.4/` — eSpeak NG 1.52.0: `https://github.com/espeak-ng/espeak-ng/releases/tag/1.52.0` |
| num2words | 0.5.14 | LGPL-2.1-or-later | `https://github.com/savoirfairelinux/num2words` (PyPI: `num2words==0.5.14`) |
| soxr | 1.0.0 | LGPL-2.1-or-later | `https://github.com/dofuuz/python-soxr` (PyPI: `python-soxr==1.0.0`) |

The full licence texts for phonemizer, num2words and soxr ship inside the built
app in their package metadata directories (`.../phonemizer-3.4.0.dist-info/`,
`.../num2words-0.5.14.dist-info/`, `.../soxr-1.0.0.dist-info/`). The GPL-3.0 text
for the bundled eSpeak NG is **not currently included** in the bundle; it can be
obtained from the eSpeak NG source link above.

These components are shipped deliberately, with their notices and source links;
a counsel-reviewed alternative is pending. This list is factual reporting of the
bundled dependencies; it is not legal advice.

## Asha itself

Asha and its own source are licensed under the MIT licence; see
[`LICENSE`](LICENSE). That licence covers our own code only. The components
listed above remain under their own licences and are distributed with their
notices. This file does not modify or limit either licence.
