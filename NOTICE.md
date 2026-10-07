# Notices

## Not affiliated

Autodesk, the Autodesk logo and Fusion (Autodesk Fusion, Fusion 360) are registered trademarks or trademarks of
Autodesk, Inc., and/or its subsidiaries and/or affiliates. Needle and Cactus are names of Cactus Compute.
Hugging Face is a trademark of Hugging Face, Inc. All other names belong to their owners.

Fusion Needle is an independent project. It is not affiliated with, sponsored by, or endorsed by Autodesk, Inc.,
Cactus Compute or Hugging Face. It talks to Autodesk Fusion only through the MCP server that Fusion itself
provides, and it contains no Autodesk software.

## Third-party components

Source checkout (installed by `pip`, not part of this repository):

| Component | Used for | Licence |
| --- | --- | --- |
| [`cactus-needle`](https://github.com/cactus-compute/needle) 3.1.1 (Cactus Compute) | Python client of the Needle engine | Apache-2.0 (package metadata and its LICENSE file) |
| `huggingface_hub` | dependency of `cactus-needle` | Apache-2.0 |
| `PyYAML` | reading the tool settings | MIT |
| `pywebview` (optional) | the app's own window | BSD-3-Clause |

Additionally inside the release builds:

| Component | Licence |
| --- | --- |
| Needle inference engine library (`libneedle`), fetched from [huggingface.co/Cactus-Compute/needle3](https://huggingface.co/Cactus-Compute/needle3) | Published by Cactus Compute; the engine wheel carries no licence metadata of its own (the `cactus-needle` package that loads it is Apache-2.0). **TODO(owner): confirm the redistribution terms before publishing builds.** |
| Python runtime and standard library | PSF-2.0 |
| PyInstaller bootloader | GPL-2.0-or-later with a special exception that allows distributing bundled applications under any licence |
| Further Python packages pulled in by the above | their licence files are kept in the build's `_internal/*.dist-info/` folders |

The model weights are not part of this repository or of the release builds. They are a fine-tune of Needle 3 by
Cactus Compute and are downloaded from [huggingface.co/CharlZKP/fusion-needle3](https://huggingface.co/CharlZKP/fusion-needle3); see the
model card there for their licence.
