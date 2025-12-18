.PHONY: help install dev-install build clean test lint format debian obs

help:
	@echo "WASM Makefile"
	@echo ""
	@echo "Usage:"
	@echo "  make install           Install WASM system-wide"
	@echo "  make dev-install       Install WASM in development mode"
	@echo "  make build             Build the package"
	@echo "  make clean             Remove build artifacts"
	@echo "  make test              Run tests"
	@echo "  make lint              Run linters"
	@echo "  make format            Format code"
	@echo ""
	@echo "Packaging:"
	@echo "  make debian            Build Debian package"
	@echo "  make ppa-upload        Build and upload to PPA (all distributions)"
	@echo "  make obs-upload        Build and upload to OBS (all distributions)"
	@echo "  make obs-status        Check OBS build status"
	@echo ""

install:
	pip install .

dev-install:
	pip install -e ".[dev]"

build:
	python -m build

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf src/*.egg-info/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .coverage
	rm -rf htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

test:
	pytest

test-cov:
	pytest --cov=wasm --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

lint:
	ruff check src/wasm tests
	mypy src/wasm

format:
	black src/wasm tests
	isort src/wasm tests
	ruff check --fix src/wasm tests

debian: clean
	dpkg-buildpackage -us -uc -b
	@echo "Debian package built in parent directory"

debian-source: clean
	dpkg-buildpackage -us -uc -S
	@echo "Source package built in parent directory"

ppa-upload: 
	@echo "Building and uploading packages to PPA..."
	./build-and-upload-ppa.sh

ppa-upload-custom:
	@echo "Building and uploading packages to PPA for custom distributions..."
	@echo "Usage: make ppa-upload-custom DISTS='noble plucky'"
	./build-and-upload-ppa.sh $(DISTS)

# OBS (Open Build Service) targets
obs-upload:
	@echo "Building and uploading packages to OBS..."
	@if [ ! -f ~/.oscrc ]; then \
		echo "Error: OSC not configured. Run: osc config set apiurl https://api.opensuse.org"; \
		exit 1; \
	fi
	chmod +x build-and-upload-obs.sh
	./build-and-upload-obs.sh

obs-upload-custom:
	@echo "Building and uploading to custom OBS project..."
	@echo "Usage: make obs-upload-custom PROJECT=home:myuser PACKAGE=wasm"
	chmod +x build-and-upload-obs.sh
	./build-and-upload-obs.sh $(PROJECT) $(PACKAGE)

obs-status:
	@echo "Checking OBS build status..."
	@if [ ! -f ~/.oscrc ]; then \
		echo "Error: OSC not configured"; \
		exit 1; \
	fi
	osc results home:Perkybeet wasm || echo "Run: make obs-upload first"

obs-status-watch:
	@echo "Watching OBS build status (Ctrl+C to stop)..."
	@if [ ! -f ~/.oscrc ]; then \
		echo "Error: OSC not configured"; \
		exit 1; \
	fi
	watch -n 10 "osc results home:Perkybeet wasm"

obs-logs:
	@echo "Fetching OBS build logs..."
	@echo "Usage: make obs-logs DISTRO=Fedora_40 ARCH=x86_64"
	@if [ -z "$(DISTRO)" ] || [ -z "$(ARCH)" ]; then \
		echo "Error: DISTRO and ARCH required"; \
		echo "Example: make obs-logs DISTRO=Fedora_40 ARCH=x86_64"; \
		exit 1; \
	fi
	osc buildlog home:Perkybeet wasm $(DISTRO) $(ARCH)
