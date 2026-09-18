# Third-party notices — bundled components

The Asha desktop app bundles the following third-party components as part of its
on-device speech stack (Moonshine STT + Kokoro TTS). Each component remains
under its own licence. Full licence texts, where available, ship inside the app
bundle next to the component; the paths below are relative to
`Contents/Resources/`.

## Python runtime — copyleft components

The following copyleft components are part of the on-device Kokoro /
`kokoro-onnx` text-to-speech stack and are deliberately bundled. Each remains
under its own licence; the app's MIT licence does not cover them and does not
relicense them.

### phonemizer 3.4.0 — GPL-3.0-or-later

- Source for this version: <https://github.com/bootphon/phonemizer> (PyPI:
  `phonemizer==3.4.0`)
- Full licence text: `runtime/lib/python3.12/site-packages/phonemizer-3.4.0.dist-info/licenses/LICENSE`

### espeakng-loader 0.2.4 — bundles eSpeak NG 1.52.0 (GPL-3.0-or-later)

- Loader package source: <https://pypi.org/project/espeakng-loader/0.2.4/> (its
  own PyPI metadata declares no licence)
- Bundled eSpeak NG library + data:
  `runtime/lib/python3.12/site-packages/espeakng_loader/`
  (`libespeak-ng.1.52.0.dylib`, `espeak-ng-data/`)
- eSpeak NG source for this version:
  <https://github.com/espeak-ng/espeak-ng/releases/tag/1.52.0>
- Full eSpeak NG GPL-3.0 text: **not currently included in the bundle** — obtain
  it from the eSpeak NG source link above.

### num2words 0.5.14 — LGPL-2.1-or-later

- Source for this version: <https://github.com/savoirfairelinux/num2words>
  (PyPI: `num2words==0.5.14`)
- Full licence text: `runtime/lib/python3.12/site-packages/num2words-0.5.14.dist-info/COPYING`

### soxr 1.0.0 — LGPL-2.1-or-later

- Source for this version: <https://github.com/dofuuz/python-soxr> (PyPI:
  `python-soxr==1.0.0`)
- Full licence text: `runtime/lib/python3.12/site-packages/soxr-1.0.0.dist-info/licenses/COPYING.LGPL`;
  additional notices ship in `LICENSE.txt`, `LICENSE-libsoxr.txt` and
  `LICENSE-PFFFT.txt` in the same directory.

## Asha's own licence

Asha itself is licensed separately under the MIT licence; see the repository
`LICENSE`. That licence covers our own code only — the components listed above
remain under their own licences and are distributed with their notices. This
file is factual reporting of the bundled dependencies; it is not legal advice.
These copyleft components are shipped deliberately with their notices and source
links; a counsel-reviewed alternative is pending.
