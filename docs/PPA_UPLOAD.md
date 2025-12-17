# PPA Upload Guide

This document explains how to build and upload WASM packages to the PPA for multiple Ubuntu distributions.

## Quick Start

The easiest way to build and upload packages for all supported distributions:

```bash
make ppa-upload
```

This will automatically build and upload packages for:
- Ubuntu 24.04 LTS Noble Numbat
- Ubuntu 24.10 Plucky Puffin  
- Ubuntu 25.04 Questing Qetzal

## Custom Distributions

To upload for specific distributions only:

```bash
./build-and-upload-ppa.sh noble plucky
```

Or using make:

```bash
make ppa-upload-custom DISTS="noble plucky"
```

## Script Features

The `build-and-upload-ppa.sh` script automates:

1. **Changelog Management**: Automatically updates `debian/changelog` for each distribution
2. **Building**: Creates source packages for each distribution
3. **Signing**: Signs packages with your GPG key
4. **Uploading**: Uploads to the PPA via dput
5. **Cleanup**: Restores original changelog and cleans up build artifacts
6. **Error Handling**: Continues with remaining distributions if one fails

## Configuration

Edit the script to customize:

```bash
PPA="ppa:yago2003/wasm"                      # Your PPA
GPG_KEY="DA2D452B1614CA82"                   # Your GPG key ID
DEFAULT_DISTRIBUTIONS=("noble" "plucky" "questing")  # Default distributions
```

## Prerequisites

Before running the script, ensure you have:

1. **GPG Key**: Registered with Launchpad
   ```bash
   gpg --list-secret-keys --keyid-format LONG
   ```

2. **PPA Access**: Your GPG key uploaded to your Launchpad account

3. **Required Tools**:
   ```bash
   sudo apt install devscripts dput
   ```

4. **PPA Configuration**: `~/.dput.cf` should have PPA settings

## Process Flow

```
For each distribution:
  ├─ Backup debian/changelog
  ├─ Update changelog for distribution
  ├─ Build source package (dpkg-buildpackage -S)
  ├─ Sign package (debsign)
  ├─ Upload to PPA (dput)
  └─ Restore changelog
```

## Version Numbering

The script automatically creates proper version numbers for each distribution:

- Base version: `0.8.2`
- Noble: `0.8.2-1~noble`
- Plucky: `0.8.2-1~plucky`
- Questing: `0.8.2-1~questing`

The `~` ensures proper version ordering in the PPA.

## Troubleshooting

### Package Already Uploaded

If you see "already been uploaded", the script will automatically retry with `--force`:

```bash
dput --force ppa:yago2003/wasm <changes-file>
```

### GPG Signing Fails

Ensure your GPG key is properly configured:

```bash
# Test signing
echo "test" | gpg --clear-sign
```

### Build Fails

Check the build log for errors:

```bash
# Manual build for debugging
dpkg-buildpackage -us -uc -S
```

### Upload Fails

Verify your PPA configuration:

```bash
# Test connection
dput --check ppa:yago2003/wasm
```

## Adding New Distributions

To add support for a new Ubuntu release:

1. Edit the script:
   ```bash
   DEFAULT_DISTRIBUTIONS=("noble" "plucky" "questing" "oracular")
   ```

2. Run the script:
   ```bash
   make ppa-upload
   ```

## Checking Upload Status

After upload, packages are queued for building on Launchpad:

1. Visit: https://launchpad.net/~yago2003/+archive/ubuntu/wasm
2. Check "Packages" tab for build status
3. Wait for builds to complete (usually 5-15 minutes)

## Example Output

```
╔════════════════════════════════════════════════════════╗
║  WASM PPA Builder - Multi-Distribution Package Upload ║
╚════════════════════════════════════════════════════════╝

[INFO] Using default distributions: noble plucky questing
[INFO] Package version: 0.8.2
[INFO] PPA: ppa:yago2003/wasm
[INFO] GPG Key: DA2D452B1614CA82

[INFO] ================================================
[INFO] Processing distribution: noble
[INFO] ================================================
[INFO] Updated changelog for distribution: noble
[INFO] Building source package for noble...
[SUCCESS] Source package built for noble
[INFO] Signing package: ../wasm_0.8.2-1~noble_source.changes
[SUCCESS] Package signed successfully
[INFO] Uploading to PPA: ppa:yago2003/wasm
[SUCCESS] Package uploaded successfully
[SUCCESS] ✓ noble processed successfully

...

[INFO] ================================================
[INFO] Summary
[INFO] ================================================
[SUCCESS] Successfully processed: 3 distribution(s)

[SUCCESS] All packages uploaded successfully! 🎉
[INFO] Packages will be available in the PPA after Launchpad builds them.
[INFO] Check status at: https://launchpad.net/~yago2003/+archive/ubuntu/wasm
```

## Manual Process (For Reference)

If you need to do it manually for one distribution:

```bash
# 1. Update changelog
sed -i '1s/questing/noble/' debian/changelog

# 2. Build source package
make debian-source

# 3. Sign
debsign -k YOUR_GPG_KEY ../wasm_VERSION_source.changes

# 4. Upload
dput ppa:yago2003/wasm ../wasm_VERSION_source.changes

# 5. Restore changelog
git checkout debian/changelog
```

## Best Practices

1. **Test Locally First**: Always test package installation locally before uploading
2. **Version Bumps**: Increment version in `pyproject.toml`, `__init__.py`, and `debian/changelog`
3. **Changelog Entries**: Write clear, descriptive changelog entries
4. **Test All Distributions**: Verify packages work on each Ubuntu version
5. **Monitor Builds**: Check Launchpad for build failures

## See Also

- [Debian Packaging Guide](https://www.debian.org/doc/manuals/maint-guide/)
- [Launchpad PPA Documentation](https://help.launchpad.net/Packaging/PPA)
- [Ubuntu Packaging Guide](https://packaging.ubuntu.com/html/)
