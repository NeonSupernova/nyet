# Releasing Nyet

This repo is **private** and stays private — it holds the compiler
source. GitHub Releases and Actions artifacts inherit repository
visibility, so nothing published from here is downloadable by the
public.

Public downloads come from a second, **public** repo:

> https://github.com/NeonSupernova/nyet-releases

It carries the README, the docs, the issue tracker, a mirror of the
demo sources, and the release assets. It contains no compiler code.
`.github/workflows/windows-package.yml` builds the bundle here and
publishes it there.

## One-time setup: the release token

The built-in `GITHUB_TOKEN` cannot write to another repository, so the
workflow needs a personal access token.

1. Create a **fine-grained** PAT at
   https://github.com/settings/personal-access-tokens/new
   - **Resource owner:** `NeonSupernova`
   - **Repository access:** *Only select repositories* →
     `nyet-releases` (this one repo, nothing else)
   - **Permissions:** `Contents: Read and write`. That covers both
     creating releases and pushing the `examples/` sync commit.
   - **Expiration:** whatever you'll remember to rotate. The workflow
     fails loudly with a clear message when the token is missing or
     expired, so a lapse is annoying rather than dangerous.
2. Add it to this repo as a secret named `RELEASE_TOKEN`:
   ```bash
   gh secret set RELEASE_TOKEN --repo NeonSupernova/nyet --body "<the token>"
   ```

Scoping it to the one public repo is deliberate: the token cannot
touch this private repo even if it leaks from a workflow log.

## Cutting a release

1. Bump `__version__` in `pynyet/__init__.py`. That single line is the
   source of truth — the workflow reads it to name the tag, and
   `nyet --version` prints it.
2. Add an entry to the **public** repo's `CHANGELOG.md` describing what
   changed for users, and push that.
3. Run the workflow:
   ```bash
   gh workflow run windows-package.yml \
     --repo NeonSupernova/nyet \
     -f publish=true
   ```
   Set `publish=false` to build and smoke-test without releasing
   anything — the bundle still lands as an Actions artifact.

   **Don't pass `prerelease=true` for a build the public README points
   at.** GitHub's `/releases/latest/` resolves to the newest
   *non*-prerelease release, so marking one as a pre-release 404s the
   permanent download link. This was hit on the very first v0.3.0
   publish; the input now defaults to false and warns if you set it.
4. The workflow then:
   - builds `nyet.exe` and fetches the WinLibs toolchain,
   - runs the full smoke-test suite against the bundled compiler,
   - stages `nyet-windows-x64.zip` plus `SHA256SUMS.txt`,
   - composes release notes that name the exact WinLibs build (see
     "GPL obligations" below),
   - creates the release at tag `v<version>` on the public repo,
   - regenerates the public `examples/` mirror from
     `packaging/stage_public_examples.py` and pushes it if it changed.

The asset name is intentionally unversioned so this link is permanent:

```
https://github.com/NeonSupernova/nyet-releases/releases/latest/download/nyet-windows-x64.zip
```

Re-running the workflow without bumping `__version__` fails on
purpose rather than replacing a download people may already have
linked.

## Two things that will bite you

Both were found by actually downloading a published release and
verifying it, which is worth doing after any change to the packaging.

- **`/releases/latest/` ignores pre-releases.** See above.
- **`Set-Content` writes CRLF.** A `SHA256SUMS.txt` with CRLF line
  endings makes `sha256sum -c` and `shasum -c` look for a file whose
  name ends in a carriage return, so verification fails on every
  non-Windows machine for an archive that is perfectly fine. The
  workflow writes it with `[System.IO.File]::WriteAllText` and an
  explicit `` `n `` for that reason — don't "simplify" it back.

After re-uploading an asset, the anonymous download URL can serve a
stale copy from GitHub's CDN for a while; `gh release download` goes
through the API and shows you the real stored file.


## GPL obligations

The bundle redistributes the WinLibs MinGW-w64 + LLVM toolchain, which
includes GPL-licensed components (GCC, binutils, and friends). Users
are entitled to the matching source. Nyet modifies none of it and
ships upstream binaries verbatim, so pointing at the WinLibs release
the build used satisfies that — which is why
`packaging/windows/fetch_clang.ps1` writes `dist/toolchain.json` and
the release notes quote it.

Two consequences worth knowing:

- **Don't hand-assemble a bundle for public distribution.** If
  `toolchain.json` is missing, the release notes cannot name the
  source, and the obligation goes unmet. The workflow always starts
  from a clean download; a local rebuild over an existing `clang\`
  tree warns about exactly this.
- **Pruning the toolchain would help twice.** `nyet` only needs clang,
  the linker, and the CRT/import libraries — not the whole GCC suite.
  Dropping the GPL'd parts would cut the ~400MB download substantially
  *and* remove the source-offer obligation. Not done yet; it's the
  single biggest download-size win available.

## Publishing the compiler source is never part of this

`packaging/stage_public_examples.py` defines exactly which files reach
the public repo, and it only ever copies `.no` sources. If you extend
it, keep it that way.
