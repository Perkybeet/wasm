#!/bin/bash
#
# build-and-upload-ppa.sh
# Automated script to build and upload WASM packages to PPA for multiple Ubuntu releases
#
# Usage:
#   ./build-and-upload-ppa.sh [distributions...]
#
# Example:
#   ./build-and-upload-ppa.sh noble plucky questing
#   ./build-and-upload-ppa.sh  # Uses default distributions
#

set -e  # Exit on error

# Configuration
PPA="ppa:yago2003/wasm"
GPG_KEY="DA2D452B1614CA82"
DEFAULT_DISTRIBUTIONS=("noble" "plucky" "questing")
CHANGELOG_FILE="debian/changelog"
PARENT_DIR=".."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Get current version from changelog
get_version() {
    head -n 1 "$CHANGELOG_FILE" | sed 's/.*(\(.*\)).*/\1/' | cut -d'~' -f1
}

# Get current distribution from changelog
get_current_distribution() {
    head -n 1 "$CHANGELOG_FILE" | sed 's/.*) \(.*\);.*/\1/'
}

# Backup changelog
backup_changelog() {
    cp "$CHANGELOG_FILE" "${CHANGELOG_FILE}.backup"
    log_info "Changelog backed up"
}

# Restore changelog from backup
restore_changelog() {
    if [ -f "${CHANGELOG_FILE}.backup" ]; then
        mv "${CHANGELOG_FILE}.backup" "$CHANGELOG_FILE"
        log_info "Changelog restored from backup"
    fi
}

# Update changelog for a specific distribution
update_changelog_for_distribution() {
    local dist=$1
    local version=$(get_version)
    
    # Update the first line to use the target distribution
    sed -i "1s/) [^;]*;/) ${dist};/" "$CHANGELOG_FILE"
    sed -i "1s/(${version}[^)]*)/(${version}~${dist})/" "$CHANGELOG_FILE"
    
    log_info "Updated changelog for distribution: ${dist}"
}

# Build source package
build_source_package() {
    local dist=$1
    
    log_info "Building source package for ${dist}..."
    make clean > /dev/null 2>&1
    make debian-source > /dev/null 2>&1
    
    if [ $? -eq 0 ]; then
        log_success "Source package built for ${dist}"
        return 0
    else
        log_error "Failed to build source package for ${dist}"
        return 1
    fi
}

# Sign package
sign_package() {
    local changes_file=$1
    
    log_info "Signing package: ${changes_file}"
    debsign -k "$GPG_KEY" "$changes_file" > /dev/null 2>&1
    
    if [ $? -eq 0 ]; then
        log_success "Package signed successfully"
        return 0
    else
        log_error "Failed to sign package"
        return 1
    fi
}

# Upload to PPA
upload_to_ppa() {
    local changes_file=$1
    
    log_info "Uploading to PPA: ${PPA}"
    
    # Check if already uploaded
    if dput "$PPA" "$changes_file" 2>&1 | grep -q "already been uploaded"; then
        log_warning "Package already uploaded, forcing re-upload..."
        dput --force "$PPA" "$changes_file" > /dev/null 2>&1
    fi
    
    if [ $? -eq 0 ]; then
        log_success "Package uploaded successfully"
        return 0
    else
        log_error "Failed to upload package"
        return 1
    fi
}

# Clean up build artifacts for a distribution
cleanup_build_artifacts() {
    local version=$1
    local dist=$2
    
    rm -f "${PARENT_DIR}/wasm_${version}~${dist}"*
    log_info "Cleaned up build artifacts for ${dist}"
}

# Process a single distribution
process_distribution() {
    local dist=$1
    local version=$(get_version)
    
    echo ""
    log_info "================================================"
    log_info "Processing distribution: ${dist}"
    log_info "================================================"
    
    # Update changelog
    update_changelog_for_distribution "$dist"
    
    # Build source package
    if ! build_source_package "$dist"; then
        log_error "Skipping ${dist} due to build failure"
        return 1
    fi
    
    # Sign package
    local changes_file="${PARENT_DIR}/wasm_${version}~${dist}_source.changes"
    if [ ! -f "$changes_file" ]; then
        log_error "Changes file not found: ${changes_file}"
        return 1
    fi
    
    if ! sign_package "$changes_file"; then
        log_error "Skipping ${dist} due to signing failure"
        return 1
    fi
    
    # Upload to PPA
    if ! upload_to_ppa "$changes_file"; then
        log_error "Failed to upload ${dist}"
        return 1
    fi
    
    log_success "✓ ${dist} processed successfully"
    return 0
}

# Main execution
main() {
    # Check for help flag
    if [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
        echo "WASM PPA Builder - Multi-Distribution Package Upload"
        echo ""
        echo "Usage:"
        echo "  $0 [distributions...]"
        echo ""
        echo "Examples:"
        echo "  $0                    # Build for all default distributions"
        echo "  $0 noble plucky       # Build only for noble and plucky"
        echo "  $0 questing           # Build only for questing"
        echo ""
        echo "Default distributions: ${DEFAULT_DISTRIBUTIONS[*]}"
        echo "PPA: ${PPA}"
        echo ""
        exit 0
    fi
    
    echo -e "${BLUE}╔════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║  WASM PPA Builder - Multi-Distribution Package Upload ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════╝${NC}"
    echo ""
    
    # Check if we're in the right directory
    if [ ! -f "$CHANGELOG_FILE" ]; then
        log_error "debian/changelog not found. Are you in the project root?"
        exit 1
    fi
    
    # Check if required tools are installed
    for tool in dpkg-buildpackage debsign dput; do
        if ! command -v "$tool" &> /dev/null; then
            log_error "Required tool not found: $tool"
            exit 1
        fi
    done
    
    # Get distributions to process
    local distributions=("$@")
    if [ ${#distributions[@]} -eq 0 ]; then
        distributions=("${DEFAULT_DISTRIBUTIONS[@]}")
        log_info "Using default distributions: ${distributions[*]}"
    else
        log_info "Using specified distributions: ${distributions[*]}"
    fi
    
    # Get current version
    local version=$(get_version)
    log_info "Package version: ${version}"
    log_info "PPA: ${PPA}"
    log_info "GPG Key: ${GPG_KEY}"
    echo ""
    
    # Backup original changelog
    backup_changelog
    
    # Track success/failure
    local success_count=0
    local failure_count=0
    local failed_dists=()
    
    # Process each distribution
    for dist in "${distributions[@]}"; do
        if process_distribution "$dist"; then
            ((success_count++))
        else
            ((failure_count++))
            failed_dists+=("$dist")
        fi
        
        # Restore changelog for next iteration
        restore_changelog
    done
    
    echo ""
    log_info "================================================"
    log_info "Summary"
    log_info "================================================"
    log_success "Successfully processed: ${success_count} distribution(s)"
    
    if [ $failure_count -gt 0 ]; then
        log_error "Failed to process: ${failure_count} distribution(s)"
        log_error "Failed distributions: ${failed_dists[*]}"
    fi
    
    echo ""
    
    if [ $failure_count -eq 0 ]; then
        log_success "All packages uploaded successfully! 🎉"
        log_info "Packages will be available in the PPA after Launchpad builds them."
        log_info "Check status at: https://launchpad.net/~yago2003/+archive/ubuntu/wasm"
        exit 0
    else
        log_warning "Some packages failed to upload. Please check the errors above."
        exit 1
    fi
}

# Cleanup function for trap
cleanup() {
    log_info "Cleaning up..."
    restore_changelog
}

# Set trap to restore changelog on exit
trap cleanup EXIT INT TERM

# Run main function
main "$@"
