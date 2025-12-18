#!/bin/bash
#
# OBS Docker Entrypoint Script
# Handles OSC configuration and provides helper commands
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║           WASM - OBS Build Environment                        ║"
echo "║                                                               ║"
echo "║  Project: ${OBS_PROJECT}/${OBS_PACKAGE}                              ║"
echo "║  API: ${OBS_API}                              ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check if OSC is configured
OSC_CONFIG="/root/.config/osc/oscrc"

configure_osc() {
    if [ ! -f "$OSC_CONFIG" ]; then
        echo -e "${YELLOW}[!] OSC not configured. Let's set it up...${NC}"
        echo ""
        
        read -p "Enter your OBS username [Perkybeet]: " OBS_USER
        OBS_USER=${OBS_USER:-Perkybeet}
        
        read -sp "Enter your OBS password: " OBS_PASS
        echo ""
        
        # Create oscrc configuration
        mkdir -p /root/.config/osc
        cat > "$OSC_CONFIG" << EOF
[general]
apiurl = ${OBS_API}
build-root = /var/tmp/build-root/%(repo)s-%(arch)s

[${OBS_API}]
user = ${OBS_USER}
pass = ${OBS_PASS}
trusted_prj = openSUSE:Factory openSUSE:Tumbleweed
EOF
        chmod 600 "$OSC_CONFIG"
        
        # Also create symlink for osc compatibility
        ln -sf "$OSC_CONFIG" ~/.oscrc 2>/dev/null || true
        
        echo -e "${GREEN}[✓] OSC configured successfully!${NC}"
    else
        echo -e "${GREEN}[✓] OSC already configured${NC}"
        # Ensure symlink exists
        ln -sf "$OSC_CONFIG" ~/.oscrc 2>/dev/null || true
    fi
}

# Show available commands
show_help() {
    echo -e "${BLUE}Available commands:${NC}"
    echo ""
    echo "  obs-upload        Upload package to OBS (interactive)"
    echo "  obs-upload-auto   Upload package to OBS (non-interactive)"
    echo "  obs-status        Check build status"
    echo "  obs-logs          View build logs"
    echo "  obs-checkout      Checkout OBS package locally"
    echo "  obs-test-build    Test build locally"
    echo "  obs-configure     Reconfigure OSC credentials"
    echo ""
    echo "  bash              Start interactive shell"
    echo "  help              Show this help"
    echo ""
}

# Upload to OBS
obs_upload() {
    echo -e "${BLUE}[*] Uploading package to OBS...${NC}"
    
    cd /workspace
    
    # Get version from changelog (e.g., "0.10.0-1~noble" -> "0.10.0-1")
    VERSION=$(head -n 1 debian/changelog 2>/dev/null | sed 's/.*(\(.*\)).*/\1/' | cut -d'~' -f1 || echo "0.10.0-1")
    # Extract base version for RPM (remove debian revision: "0.10.0-1" -> "0.10.0")
    VERSION_BASE=$(echo "${VERSION}" | cut -d'-' -f1)
    echo -e "${BLUE}[*] Version: ${VERSION} (base: ${VERSION_BASE})${NC}"
    
    # Create tarball (use base version for tarball name - required for RPM)
    # Exclude debian/ folder as OBS builds it from debian.* files
    echo -e "${BLUE}[1/5] Creating source tarball...${NC}"
    TARBALL="wasm-${VERSION_BASE}.tar.gz"
    git archive --format=tar.gz --prefix=wasm-${VERSION_BASE}/ HEAD ':!debian' > "/tmp/${TARBALL}"
    echo -e "${GREEN}[✓] Created ${TARBALL}${NC}"
    
    # Checkout OBS package
    echo -e "${BLUE}[2/5] Checking out OBS package...${NC}"
    cd /tmp
    rm -rf "${OBS_PROJECT}" 2>/dev/null || true
    
    if ! osc checkout "${OBS_PROJECT}/${OBS_PACKAGE}" 2>/dev/null; then
        echo -e "${YELLOW}[!] Package doesn't exist, creating...${NC}"
        osc checkout "${OBS_PROJECT}"
        cd "${OBS_PROJECT}"
        osc mkpac "${OBS_PACKAGE}"
    fi
    
    cd "/tmp/${OBS_PROJECT}/${OBS_PACKAGE}"
    echo -e "${GREEN}[✓] Package checked out${NC}"
    
    # Remove _service if it exists (causes issues with Debian/Ubuntu builds)
    if [ -f "_service" ]; then
        osc rm _service 2>/dev/null || rm -f _service
    fi
    
    # Remove debian.compat if exists (conflicts with debhelper-compat in control)
    if [ -f "debian.compat" ]; then
        osc rm debian.compat 2>/dev/null || rm -f debian.compat
    fi
    
    # Remove old tarballs (we're using a new naming scheme)
    for old_tarball in wasm-*.tar.gz; do
        if [ -f "$old_tarball" ] && [ "$old_tarball" != "${TARBALL}" ]; then
            echo -e "${YELLOW}[!] Removing old tarball: $old_tarball${NC}"
            osc rm "$old_tarball" 2>/dev/null || rm -f "$old_tarball"
        fi
    done
    
    # Copy files
    echo -e "${BLUE}[3/5] Copying package files...${NC}"
    cp "/tmp/${TARBALL}" .
    cp /workspace/rpm/wasm.spec .
    
    # Copy OBS-specific debian files (from obs/ folder)
    # NOTE: debian.compat is NOT copied - we use debhelper-compat in Build-Depends
    if [ -f /workspace/obs/debian.control ]; then
        cp /workspace/obs/debian.control .
        cp /workspace/obs/debian.rules .
        cp /workspace/obs/debian.changelog .
        cp /workspace/obs/debian.copyright .
        cp /workspace/obs/debian.postinst . 2>/dev/null || true
        cp /workspace/obs/debian.postrm . 2>/dev/null || true
        # Config file: copy from debian/ (single source of truth)
        cp /workspace/debian/wasm.default.yaml debian.wasm.default.yaml 2>/dev/null || true
        # Same file for RPM (used as Source1)
        cp /workspace/debian/wasm.default.yaml . 2>/dev/null || true
        # Man page for RPM (used as Source2)
        cp /workspace/debian/wasm.1 . 2>/dev/null || true
        cp /workspace/obs/wasm.dsc .
        # Update version in .dsc
        sed -i "s/^Version:.*/Version: ${VERSION}/" wasm.dsc
    else
        # Fallback to debian/ folder
        cp /workspace/debian/control debian.control 2>/dev/null || true
        cp /workspace/debian/rules debian.rules 2>/dev/null || true
        cp /workspace/debian/changelog debian.changelog 2>/dev/null || true
        cp /workspace/debian/copyright debian.copyright 2>/dev/null || true
        cp /workspace/debian/compat debian.compat 2>/dev/null || true
        
        # Create .dsc file for Debian builds
        cat > "wasm.dsc" << EOF
Format: 3.0 (native)
Source: wasm
Binary: wasm
Architecture: all
Version: ${VERSION}
Maintainer: Yago López Prado <yago.lopez.adeje@gmail.com>
Homepage: https://github.com/Perkybeet/wasm
Standards-Version: 4.6.0
Build-Depends: debhelper-compat (= 13), dh-python, python3-all, python3-setuptools, pybuild-plugin-pyproject, python3-pip, python3-wheel
Package-List:
 wasm deb admin optional arch=all
Files:
 00000000000000000000000000000000 0 wasm_${VERSION}.tar.gz
EOF
    fi
    
    # Update version in spec (use base version without debian revision)
    sed -i "s/^Version:.*/Version:        ${VERSION_BASE}/" wasm.spec
    echo -e "${GREEN}[✓] Files copied${NC}"
    
    # Add files to OBS
    echo -e "${BLUE}[4/5] Adding files to OBS...${NC}"
    osc add "${TARBALL}" 2>/dev/null || true
    osc add wasm.spec 2>/dev/null || true
    osc add wasm.dsc 2>/dev/null || true
    osc add debian.control 2>/dev/null || true
    osc add debian.rules 2>/dev/null || true
    osc add debian.changelog 2>/dev/null || true
    osc add debian.copyright 2>/dev/null || true
    osc add debian.postinst 2>/dev/null || true
    osc add debian.postrm 2>/dev/null || true
    osc add debian.wasm.default.yaml 2>/dev/null || true
    osc add wasm.default.yaml 2>/dev/null || true
    osc add wasm.1 2>/dev/null || true
    
    echo -e "${BLUE}[*] Files to commit:${NC}"
    osc status
    
    # Commit
    echo -e "${BLUE}[5/5] Committing to OBS...${NC}"
    osc commit -m "Update to version ${VERSION}"
    
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  ✓ Package uploaded successfully!                             ║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${CYAN}View build status:${NC}"
    echo "  https://build.opensuse.org/package/show/${OBS_PROJECT}/${OBS_PACKAGE}"
    echo ""
    echo -e "${CYAN}Or run:${NC}"
    echo "  obs-status"
    echo ""
}

# Check build status
obs_status() {
    echo -e "${BLUE}[*] Checking build status for ${OBS_PROJECT}/${OBS_PACKAGE}...${NC}"
    echo ""
    osc results "${OBS_PROJECT}" "${OBS_PACKAGE}"
}

# View build logs
obs_logs() {
    echo -e "${BLUE}Available repositories:${NC}"
    osc results "${OBS_PROJECT}" "${OBS_PACKAGE}"
    echo ""
    read -p "Enter repository (e.g., Fedora_40): " REPO
    read -p "Enter architecture [x86_64]: " ARCH
    ARCH=${ARCH:-x86_64}
    
    osc buildlog "${OBS_PROJECT}" "${OBS_PACKAGE}" "${REPO}" "${ARCH}"
}

# Checkout package
obs_checkout() {
    echo -e "${BLUE}[*] Checking out ${OBS_PROJECT}/${OBS_PACKAGE}...${NC}"
    cd /tmp
    rm -rf "${OBS_PROJECT}" 2>/dev/null || true
    osc checkout "${OBS_PROJECT}/${OBS_PACKAGE}"
    echo -e "${GREEN}[✓] Package checked out to /tmp/${OBS_PROJECT}/${OBS_PACKAGE}${NC}"
}

# Test local build
obs_test_build() {
    echo -e "${BLUE}[*] Testing local build...${NC}"
    cd /workspace
    
    # Build RPM locally
    rpmbuild -ba rpm/wasm.spec \
        --define "_sourcedir /workspace" \
        --define "_specdir /workspace/rpm" \
        --define "_builddir /tmp/rpmbuild" \
        --define "_rpmdir /tmp/rpms" \
        --define "_srcrpmdir /tmp/srpms"
}

# Main logic
case "${1:-help}" in
    bash|sh)
        configure_osc
        show_help
        exec /bin/bash
        ;;
    obs-upload)
        configure_osc
        obs_upload
        ;;
    obs-upload-auto)
        obs_upload
        ;;
    obs-status)
        configure_osc
        obs_status
        ;;
    obs-logs)
        configure_osc
        obs_logs
        ;;
    obs-checkout)
        configure_osc
        obs_checkout
        ;;
    obs-test-build)
        obs_test_build
        ;;
    obs-configure)
        rm -f ~/.oscrc
        configure_osc
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        exec "$@"
        ;;
esac
