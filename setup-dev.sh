#!/bin/bash
# 🚀 Complete development environment setup for DeepCommit
# This script sets up everything: Python env, direnv, git hooks, etc.

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
log_info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
log_success() { echo -e "${GREEN}✅ $1${NC}"; }
log_warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }
log_error() { echo -e "${RED}❌ $1${NC}"; }

echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}     🚀 DeepCommit Development Environment Setup${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}\n"

# Step 1: Check and setup .envrc configuration
log_info "Step 1: Setting up environment variables..."
if [ ! -f ".envrc" ]; then
    log_warning ".envrc not found. Creating from template..."
    cat > .envrc << 'EOF'
# DeepCommit environment configuration.
# Edit this file, then run `direnv allow` to activate.

# GitHub crawler tokens — required for data collection. The crawler is
# rate-limited, so pass several comma-separated tokens to avoid throttling.
export GITHUB_CRAWLER_TOKENS="${GITHUB_CRAWLER_TOKENS:-}"

# Where all data is stored — needs plenty of disk; defaults to $PWD/DATA.
export DEEPCOMMIT_DATA_ROOT="$PWD/DATA"
export DEEPCOMMIT_REPO_ROOT="$PWD/DATA/github_data/repos"
export DEEPCOMMIT_PREPROCESSED_ROOT="$PWD/DATA/github_data/preprocessed"

export UV_CACHE_DIR="$PWD/.uv-cache"
export VIRTUAL_ENV="$PWD/.venv"
PATH_add "$VIRTUAL_ENV/bin"
EOF
    log_warning "Please edit .envrc and set GITHUB_CRAWLER_TOKENS!"
else
    log_success ".envrc already exists"
fi

# Step 2: Install and setup direnv
log_info "Step 2: Setting up direnv for auto-loading..."
if ! command -v direnv &> /dev/null; then
    log_warning "direnv not installed. Installing..."

    # Detect OS and install direnv
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command -v apt-get &> /dev/null; then
            log_info "Installing direnv via apt..."
            sudo apt-get update && sudo apt-get install -y direnv
        elif command -v yum &> /dev/null; then
            log_info "Installing direnv via yum..."
            sudo yum install -y direnv
        else
            log_info "Installing direnv from binary..."
            curl -sfL https://direnv.net/install.sh | bash
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &> /dev/null; then
            log_info "Installing direnv via Homebrew..."
            brew install direnv
        else
            log_error "Please install Homebrew first: https://brew.sh"
            exit 1
        fi
    fi
fi

# Setup direnv hook in shell config
SHELL_NAME=$(basename "$SHELL")
SHELL_RC=""

case "$SHELL_NAME" in
    bash)
        SHELL_RC="$HOME/.bashrc"
        HOOK_CMD='eval "$(direnv hook bash)"'
        ;;
    zsh)
        SHELL_RC="$HOME/.zshrc"
        HOOK_CMD='eval "$(direnv hook zsh)"'
        ;;
    fish)
        SHELL_RC="$HOME/.config/fish/config.fish"
        HOOK_CMD='direnv hook fish | source'
        ;;
esac

if [ -n "$SHELL_RC" ]; then
    if ! grep -q "direnv hook" "$SHELL_RC" 2>/dev/null; then
        log_info "Adding direnv hook to $SHELL_RC..."
        echo "" >> "$SHELL_RC"
        echo "# direnv hook for DeepCommit" >> "$SHELL_RC"
        echo "$HOOK_CMD" >> "$SHELL_RC"
        log_success "direnv hook added"
        log_warning "Remember to run: source $SHELL_RC"
    else
        log_success "direnv hook already configured"
    fi
fi

# Allow direnv for this directory
if command -v direnv &> /dev/null; then
    log_info "Authorizing .envrc..."
    direnv allow .
    log_success "direnv configured and authorized"
fi

# Step 3: Install uv if not installed
log_info "Step 3: Setting up uv package manager..."
if ! command -v uv &> /dev/null; then
    log_info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh

    # Add to PATH if needed
    export PATH="$HOME/.local/bin:$PATH"

    if ! command -v uv &> /dev/null; then
        log_error "uv installation failed. Please install manually."
        exit 1
    fi
    log_success "uv installed successfully"
else
    log_success "uv already installed: $(uv --version)"
fi

# Step 4: Create Python virtual environment
log_info "Step 4: Creating Python virtual environment..."
if [ ! -d ".venv" ]; then
    uv venv
    log_success "Virtual environment created"
else
    log_success "Virtual environment already exists"
fi

# Step 5: Install Python dependencies
log_info "Step 5: Installing Python dependencies..."
source .venv/bin/activate

log_info "Installing project dependencies..."
uv pip install -e .

log_info "Installing development tools..."
uv pip install -e ".[dev]" 2>/dev/null || uv pip install pytest ruff mypy pre-commit ipython

log_info "Syncing uv environment..."
uv sync

log_success "All Python packages installed"

# Step 6: Create necessary directories
log_info "Step 6: Creating project directories..."
mkdir -p DATA/github_data/repos
mkdir -p DATA/github_data/preprocessed
mkdir -p .vscode
mkdir -p bin
log_success "Project directories created"

# Step 7: Install Code Maat (code analysis tool)
log_info "Step 7: Setting up Code Maat..."

# Check if Java is available
if ! command -v java &> /dev/null; then
    log_warning "Java not found. Code Maat requires Java to run."
    log_info "Please install Java (e.g., sudo apt-get install openjdk-11-jre)"
else
    log_success "Java found: $(java -version 2>&1 | head -n1)"

    # Check if Code Maat already exists
    CODE_MAAT_JAR="bin/code-maat-1.0.4-standalone.jar"
    if [ -f "$CODE_MAAT_JAR" ]; then
        log_success "Code Maat already installed"
    else
        log_info "Downloading Code Maat..."
        CODE_MAAT_URL="https://github.com/adamtornhill/code-maat/releases/download/v1.0.4/code-maat-1.0.4-standalone.jar"

        if command -v wget &> /dev/null; then
            wget -q "$CODE_MAAT_URL" -O "$CODE_MAAT_JAR"
        elif command -v curl &> /dev/null; then
            curl -sL "$CODE_MAAT_URL" -o "$CODE_MAAT_JAR"
        else
            log_error "Neither wget nor curl found. Cannot download Code Maat."
            log_info "Please install wget or curl, or manually download from:"
            log_info "$CODE_MAAT_URL"
        fi

        if [ -f "$CODE_MAAT_JAR" ]; then
            log_success "Code Maat downloaded successfully ($(du -h $CODE_MAAT_JAR | cut -f1))"

            # Verify it works
            if java -jar "$CODE_MAAT_JAR" -h &> /dev/null; then
                log_success "Code Maat verified and ready to use"
            else
                log_warning "Code Maat downloaded but may not work correctly"
            fi
        else
            log_warning "Code Maat download failed - continuing without it"
        fi
    fi

    # Add bin/ to .gitignore if not already there
    if [ -f ".gitignore" ]; then
        if ! grep -q "^bin/" .gitignore; then
            echo "bin/" >> .gitignore
            log_success "Added bin/ to .gitignore"
        fi
    fi
fi

# Step 8: Install git hooks via the pre-commit framework (.pre-commit-config.yaml)
log_info "Step 8: Installing git hooks..."
if [ -d ".git" ]; then
    if uv run pre-commit install >/dev/null 2>&1; then
        log_success "pre-commit hooks installed"
    else
        log_warning "Could not install pre-commit hooks (run 'uv run pre-commit install' manually)"
    fi
else
    log_info "Not a git repository, skipping git hooks"
fi

# Step 9: Verify installation
echo -e "\n${GREEN}═══════════════════════════════════════════════════════════${NC}"
log_info "Step 9: Verifying installation..."

# Check Python
if source .venv/bin/activate 2>/dev/null; then
    log_success "Python: $(python --version) at $(which python)"
else
    log_warning "Python virtual environment not activated"
fi

# Check key packages
for pkg in pygit2 requests tqdm ruff; do
    if python -c "import $pkg" 2>/dev/null; then
        log_success "$pkg: installed"
    else
        log_warning "$pkg: not installed"
    fi
done

# Check Code Maat
if [ -f "bin/code-maat-1.0.4-standalone.jar" ] && command -v java &> /dev/null; then
    log_success "Code Maat: installed and Java available"
elif [ -f "bin/code-maat-1.0.4-standalone.jar" ]; then
    log_warning "Code Maat: installed but Java not found"
else
    log_warning "Code Maat: not installed"
fi

# Check environment variables
source .envrc 2>/dev/null || true
if [ -n "${GITHUB_CRAWLER_TOKENS:-}" ]; then
    log_success "GitHub crawler tokens: configured"
else
    log_warning "GitHub crawler tokens: NOT configured - set GITHUB_CRAWLER_TOKENS in .envrc"
fi

# Final summary
echo -e "\n${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}🎉 Development environment setup complete!${NC}\n"

echo -e "${BLUE}Quick start commands:${NC}"
echo -e "  1. Configure GitHub crawler tokens (if needed):"
echo -e "     ${YELLOW}nano .envrc${NC}"
echo ""
echo -e "  2. Reload shell (for direnv):"
echo -e "     ${YELLOW}source ~/.bashrc${NC} (or ~/.zshrc)"
echo ""
echo -e "  3. Activate environment:"
echo -e "     ${YELLOW}source .venv/bin/activate${NC}"
echo -e "     Or just: ${YELLOW}cd .${NC} (direnv will auto-activate)"
echo ""

if [ -z "${GITHUB_CRAWLER_TOKENS:-}" ]; then
    echo -e "${RED}⚠️  IMPORTANT: set GITHUB_CRAWLER_TOKENS in .envrc before running the crawler!${NC}"
fi