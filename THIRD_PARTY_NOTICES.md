# Third-party notices - Igdrasyl (formerly TopologyOperator)

This project is licensed under the GNU GPL v3 (see `LICENSE`). It ships or uses the following third-party material.
Licenses were read from the files in this repository and from the installed packages (`pip-licenses`, November 2026);
check them again when a dependency is updated.

## Included in the repository

| Component | Where | License | Notes |
| --- | --- | --- | --- |
| noVNC core (Copyright The noVNC Authors) | `app/static/vendor/novnc/core` | MPL-2.0 | File-level copyleft: keep the license header in each file and publish changes to those files. Compatible with GPL-3.0 (MPL-2.0 section 3.3). Full text and notice: `app/static/vendor/novnc/LICENSE.txt`, `AUTHORS`. |
| pako (Copyright Vitaly Puzrin) | `app/static/vendor/novnc/vendor/pako` | MIT | License text: `app/static/vendor/novnc/vendor/pako/LICENSE`. |
| GNS3 device icons | `app/static/vendor/gns3-icons/*.svg` | GPL (the SVG metadata names the Creative Commons "GNU GPL 2.0" license, "Created for the GNS-3 project") | Attribution and source notice required. Separate artwork files, not linked code. |

## Not included, downloaded by each user

- VMware WMKS SDK (`wmks.min.js`, `wmks-all.css`, `wmks.js`): proprietary Broadcom/VMware files. They are git-ignored and
  must never be committed or redistributed; see `spikes/webmks/README.md`.

## Python dependencies (installed, not copied)

Pinned in `pyproject.toml`: FastAPI (MIT), Starlette (BSD-3-Clause), uvicorn (BSD-3-Clause), netmiko (MIT),
python-multipart (Apache-2.0), websockets (BSD-3-Clause), Mjonyr, repository TopologyBuilder (GPL-3.0) and, through them, among others:
paramiko (LGPL-2.1) and scp (LGPL), certifi (MPL-2.0), pyvmomi (Apache-2.0), cryptography (Apache-2.0 or BSD-3-Clause),
requests (Apache-2.0), PyYAML (MIT). None of them is incompatible with GPL-3.0; the LGPL libraries are used as unmodified
libraries.
