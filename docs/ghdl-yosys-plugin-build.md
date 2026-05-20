# ghdl-yosys-plugin Build Guide

## macOS aarch64

### Prerequisites

- Yosys installed via Homebrew: `brew install yosys`
- ghdl 6.0.0 installed via Homebrew cask: `brew install --cask ghdl`

After installing ghdl, bypass macOS Gatekeeper:
```sh
sudo xattr -dr com.apple.quarantine /opt/homebrew/Caskroom/ghdl/6.0.0/ghdl-llvm-6.0.0-macos15-aarch64/bin/
```

### Build

The HEAD commit of the plugin uses a ghdl API not yet in 6.0.0. Check out the last compatible commit first:

```sh
cd ghdl-yosys-plugin
git checkout 07a30ed
```

Then build, overriding the library path (the brew cask does not symlink the dylib into `/opt/homebrew/lib`):

```sh
make LIBGHDL_LIB=/opt/homebrew/Caskroom/ghdl/6.0.0/ghdl-llvm-6.0.0-macos15-aarch64/lib/libghdl-6_0_0.dylib
```

### Install (optional)

Copies `ghdl.so` into yosys's plugin directory so `-m ghdl` works without a path:

```sh
make install LIBGHDL_LIB=/opt/homebrew/Caskroom/ghdl/6.0.0/ghdl-llvm-6.0.0-macos15-aarch64/lib/libghdl-6_0_0.dylib
```

---

## Linux aarch64 (Fedora/Asahi)

Tested with yosys 0.63 and ghdl 5.1.1.

### Prerequisites

```sh
sudo dnf install yosys yosys-devel redhat-rpm-config
```

ghdl is available as an RPM package:
```sh
sudo dnf install ghdl
```

### Build

Commits after `a8883f5` (2025-12-21) use a ghdl API not present in ghdl 5.1.1. Check out the last compatible commit first:

```sh
cd ghdl-yosys-plugin
git checkout a8883f5
```

Then build, pointing at the versioned shared library:

```sh
make LIBGHDL_LIB=/usr/lib64/libghdl-5_1_1.so
```

Output: `ghdl.so` in the current directory.

### Install (optional)

Copies `ghdl.so` into yosys's plugin directory so `-m ghdl` works without a path:

```sh
make install LIBGHDL_LIB=/usr/lib64/libghdl-5_1_1.so
```

---

## Usage

```sh
yosys -m ./ghdl.so -p 'ghdl <sources> -e <top>; synth_...'
```
