# Repository instructions

These instructions apply to every coding agent and every development machine
working on this repository.

## Releases

- The canonical distribution location is the repository's GitHub Releases page:
  <https://github.com/awisbay/trfs-automation-desktop/releases>.
- Never treat a local `dist/` file, chat attachment, branch artifact, or another
  hosting location as a completed release. A release is complete only after its
  matching tag and artifacts are visible on the canonical GitHub Releases page.
- `src/version.py` is the single source of truth. Before releasing, update
  `__version__` using semantic versioning and add a matching top entry to
  `CHANGELOG.md`.
- Release tags must be `v<version>` (for example, version `1.7.1` uses tag
  `v1.7.1`) and must point to the intended commit on `main`.
- Push the commit to `main`, then create and push the matching tag. The
  `.github/workflows/release.yml` workflow builds the Windows executable and
  publishes it automatically to the canonical GitHub Releases page.
- Confirm that the GitHub Actions run succeeded and that the release contains
  `NodeCraft-<version>-windows.exe`. Do not report the release as finished
  until both checks pass.
- Do not commit release binaries, credentials, `config.yaml`, `license.key`, or
  private signing keys.
