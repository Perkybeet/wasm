#!/bin/bash
#
# build-and-upload-obs.sh
# Automated script to build and upload WASM packages to OBS (Open Build Service)
#
# Usage:
#   ./build-and-upload-obs.sh [project] [package]
#
# Example:
#   ./build-and-upload-obs.sh home:yago2003 wasm
#   ./build-and-upload-obs.sh  # Uses defaults from config
#

set -e

# Configuration
OBS_PROJECT="${1:-home:Perkybeet}"
OBS_PACKAGE="${2:-wasm}"
OBS_API="https://api.opensuse.org"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if osc is installed
if ! command -v osc &> /dev/null; then
    log_error "osc (Open Build Service command line tool) is not installed"
    echo "Install with: sudo apt install osc"
    echo "Or: pip install osc"
    exit 1
fi

# Check if configured
if [ ! -f ~/.oscrc ]; then
    log_error "OSC not configured. Run: osc config set apiurl $OBS_API"
    exit 1
fi

# Get version
VERSION=$(head -n 1 debian/changelog | sed 's/.*(\(.*\)).*/\1/' | cut -d'~' -f1)
log_info "Building version: $VERSION"

# Create tarball
log_info "Creating source tarball..."
TARBALL="wasm-${VERSION}.tar.gz"
git archive --format=tar.gz --prefix=wasm-${VERSION}/ HEAD > "../$TARBALL"
log_success "Created $TARBALL"

# Checkout OBS package
log_info "Checking out OBS package $OBS_PROJECT/$OBS_PACKAGE..."
TMP_DIR=$(mktemp -d)
cd "$TMP_DIR"

if ! osc checkout "$OBS_PROJECT/$OBS_PACKAGE" 2>/dev/null; then
    log_error "Failed to checkout package. Does it exist?"
    echo "Create it first at: https://build.opensuse.org/project/show/$OBS_PROJECT"
    echo "Or run: osc mkpac $OBS_PROJECT $OBS_PACKAGE"
    rm -rf "$TMP_DIR"
    exit 1
fi

cd "$OBS_PROJECT/$OBS_PACKAGE"

# Copy files
log_info "Copying package files..."
cp "$OLDPWD/../$TARBALL" .
cp "$OLDPWD/rpm/wasm.spec" .
cp "$OLDPWD/obs/_service" .

# Update version in spec file
log_info "Updating version in spec file..."
sed -i "s/^Version:.*/Version:        $VERSION/" wasm.spec

# Add all files
osc add "$TARBALL" wasm.spec _service 2>/dev/null || true

# Show changes
log_info "Changes to be committed:"
osc status

# Commit
log_info "Committing to OBS..."
osc commit -m "Update to version $VERSION"

log_success "Package uploaded to OBS!"
echo ""
echo "View build status at:"
echo "  https://build.opensuse.org/package/show/$OBS_PROJECT/$OBS_PACKAGE"
echo ""
echo "Monitor builds with:"
echo "  osc results $OBS_PROJECT $OBS_PACKAGE"
echo ""
echo "Or watch live:"
echo "  watch -n 10 \"osc results $OBS_PROJECT $OBS_PACKAGE\""

# Cleanup
cd "$OLDPWD"
rm -rf "$TMP_DIR"
rm "../$TARBALL"

log_success "Done!"
