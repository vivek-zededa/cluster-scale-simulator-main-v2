.PHONY: help build-agent build-docker setup-python run-simulator cleanup push-agent setup-python-if-needed

# --- Makefile Configuration Variables ---

# Detect Jenkins environment
# BUILD_ID is a Jenkins built-in environment variable
JENKINS := $(if $(BUILD_ID),true,false)

# Define NETRC_FILE_PATH.
NETRC_FILE_PATH ?= $(HOME)/.netrc

# Jenkins environment variable for PR builds
CHANGE_ID := $(or $(CHANGE_ID),)

# GIT_TAG_NAME from Jenkins environment
GIT_TAG_NAME := $(or $(GIT_TAG_NAME),)

# Determine the Git branch name (works in Jenkins or locally)
# In Jenkins multibranch pipelines, BRANCH_NAME is automatically set
BRANCH_NAME ?= $(shell \
  if [ -n "$(CHANGE_ID)" ]; then \
    echo "pr-$(CHANGE_ID)"; \
  else \
    git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown-branch"; \
  fi \
)

# Determine if the local repo is dirty
DIRTY := $(shell git status --porcelain 2>/dev/null | grep -q . && echo -dirty || echo)

# Get short commit SHA, robustly
GIT_SHORT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo "no-sha")

# Image configuration
IMAGE_REGISTRY ?= adib146
IMAGE_NAME ?= cluster-agent-simulator

# Check if BRANCH_NAME looks like a tag (starts with v and contains a dot)
IS_TAG := $(shell echo "$(BRANCH_NAME)" | grep -E '^v[0-9]+\.' >/dev/null 2>&1 && echo true || echo false)

# Default values, to be overridden by conditions
DOCKER_TAG :=
SHOULD_PUSH := false

ifeq ($(JENKINS),true)
  ifneq ($(GIT_TAG_NAME),)
    # Jenkins provides GIT_TAG_NAME for tag builds
    DOCKER_TAG := $(GIT_TAG_NAME)
    SHOULD_PUSH := true
  else ifeq ($(IS_TAG),true)
    # BRANCH_NAME looks like a tag (e.g., v0.1.1)
    DOCKER_TAG := $(BRANCH_NAME)
    SHOULD_PUSH := true
  else ifneq ($(CHANGE_ID),)
    # PR build
    DOCKER_TAG := pr-$(CHANGE_ID)
    SHOULD_PUSH := false
  else ifeq ($(BRANCH_NAME),main)
    # Main branch build
    JENKINS_MAIN_TAG_DESC := $(shell git describe --tags --long 2>/dev/null || printf "v0.0.0-%s-g%s" "$$(git rev-list --count HEAD)" "$$(git rev-parse --short HEAD)")
    DOCKER_TAG := $(JENKINS_MAIN_TAG_DESC)
    SHOULD_PUSH := true
  else
    # Feature branch build
    DOCKER_TAG := $(BRANCH_NAME)-$(GIT_SHORT_SHA)
    SHOULD_PUSH := false
  endif
else
  # Local builds
  ifeq ($(BRANCH_NAME),main)
    LOCAL_MAIN_TAG_DESC := $(shell git describe --tags --long 2>/dev/null || printf "v0.0.0-%s-g%s" "$$(git rev-list --count HEAD)" "$$(git rev-parse --short HEAD)")
    DOCKER_TAG := $(LOCAL_MAIN_TAG_DESC)$(DIRTY)
  else
    DOCKER_TAG := $(BRANCH_NAME)$(DIRTY)
    SHOULD_PUSH := false
  endif
endif

IMAGE_TAG ?= $(DOCKER_TAG)
IMG ?= $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

# CONTAINER_TOOL defines the container tool to be used for building images.
CONTAINER_TOOL ?= docker

# Directory Configuration
SIMULATOR_DIR := shared-kwok-simulator
AGENT_DIR := $(SIMULATOR_DIR)/agent-simulator/zks-agent-sim
KWOK_DIR := $(SIMULATOR_DIR)/kwok-simulator

# Simulator Configuration
CONFIG ?= my-config.json
NUM_CLUSTERS ?= 1
START_INDEX ?= 1
NUM_NODES ?= 1
CLUSTER_PREFIX ?= kwok
CLUSTER_SUFFIX ?= scale-test



help:
	@echo "ZKS Scale Simulator - Makefile Targets"
	@echo ""
	@echo "Build & Setup:"
	@echo "  make build-agent         Build zks-agent-sim Go binary"
	@echo "  make docker-build        Build docker image (for CI)"
	@echo "  make docker-push         Push docker image (conditional)"
	@echo "  make docker-build-push   Build and push (Jenkins workflow)"
	@echo "  make push-agent          Push multi-arch agent image (manual releases)"
	@echo "  make setup-python        Setup Python virtual environment"
	@echo ""
	@echo "Run & Test:"
	@echo "  make run-simulator       Run simulator (default: 1 cluster, 1 node)"
	@echo "  make cleanup-cluster     Remove specific cluster (requires CLUSTER=name)"
	@echo "  make cleanup             Remove all simulated clusters and resources"
	@echo ""
	@echo "Configuration Variables:"
	@echo "  CONFIG            Config file path (default: my-config.json)"
	@echo "  NUM_CLUSTERS      Number of clusters (default: 1)"
	@echo "  START_INDEX       Starting cluster number (default: 1)"
	@echo "  NUM_NODES         Nodes per cluster (default: 1)"
	@echo "  CLUSTER_PREFIX    Cluster name prefix (default: kwok)"
	@echo "  CLUSTER_SUFFIX    Cluster name suffix (default: scale-test)"
	@echo "  MAX_WORKERS       Parallel workers for cluster operations (default: auto)"
	@echo "  NO_PARALLEL       Disable parallel execution (set to 1)"
	@echo "  IMAGE_REGISTRY    Registry/username (default: adib146)"
	@echo "  IMAGE_NAME        Repository name (default: cluster-agent-simulator)"
	@echo "  IMAGE_TAG         Image tag (auto-detected or manual)"
	@echo ""
	@echo "Examples:"
	@echo "  make run-simulator NUM_CLUSTERS=5 NUM_NODES=3"
	@echo "  make run-simulator NUM_CLUSTERS=10 START_INDEX=11"
	@echo "  make run-simulator NUM_CLUSTERS=50 MAX_WORKERS=10"
	@echo "  make run-simulator CLUSTER_PREFIX=prod CLUSTER_SUFFIX=test"
	@echo "  make cleanup-cluster CLUSTER=zks-scale-test-200"
	@echo "  make push-agent IMAGE_TAG=shared-v56"
	@echo ""
	@echo "Cluster Naming: {cluster_name_prefix}-{number}"
	@echo "  Default config: zks-scale-test-1, zks-scale-test-2, ..."
	@echo "  With CLUSTER_PREFIX/SUFFIX: {prefix}-{suffix}-1, {prefix}-{suffix}-2, ..."
	@echo ""

build-agent:
	@echo "Building cluster-agent-simulator..."
	cd $(AGENT_DIR) && go build -o cluster-agent-simulator

docker-build: ## Build docker image with the agent (Go build happens inside Docker).
	@echo "------------------------------"
	@echo "Docker Tag: $(DOCKER_TAG)"
	@echo "Image: $(IMG)"
	@echo "Should Push Image: $(SHOULD_PUSH)"
	@echo "Branch: $(BRANCH_NAME)"
	@echo "Git SHA: $(GIT_SHORT_SHA)"
	@echo "------------------------------"
	@echo "Building Docker image: ${IMG}"
	$(CONTAINER_TOOL) build -t ${IMG} $(AGENT_DIR)

docker-push: ## Push docker image with the agent.
ifeq ($(SHOULD_PUSH),true)
	@echo "Pushing Docker image: ${IMG}"
	$(CONTAINER_TOOL) push ${IMG}
else
	@echo "Skipping push (SHOULD_PUSH is false). Image: ${IMG}"
endif

docker-build-push: docker-build docker-push ## Build and push docker image (conditionally based on SHOULD_PUSH).

push-agent: ## Push multi-arch agent image to registry (manual releases)
	@echo "Building and pushing multi-arch image: $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"
	cd $(AGENT_DIR) && docker buildx build --platform linux/amd64,linux/arm64 -t $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) --push .
	@echo "Successfully pushed $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"

setup-python:
	@echo "Setting up Python virtual environment..."
	cd $(KWOK_DIR) && python3 -m venv venv
	cd $(KWOK_DIR) && . venv/bin/activate && pip install -r requirements.txt
	@echo "Virtual environment created at $(KWOK_DIR)/venv"

setup-python-if-needed:
	@if [ ! -d "$(KWOK_DIR)/venv" ]; then \
		echo "Virtual environment not found, creating..."; \
		$(MAKE) setup-python; \
	fi

run-simulator: setup-python-if-needed
	@echo "Running simulator: $(NUM_CLUSTERS) cluster(s), $(NUM_NODES) node(s) each, starting from index $(START_INDEX)"
	@echo "Cluster naming: $(CLUSTER_PREFIX)-$(CLUSTER_SUFFIX)-{$(START_INDEX)..$$(( $(START_INDEX) + $(NUM_CLUSTERS) - 1 ))}"
	@if [ "$(CLUSTER_PREFIX)" != "kwok" ] || [ "$(CLUSTER_SUFFIX)" != "scale-test" ]; then \
		jq '.cluster_name_prefix = "$(CLUSTER_PREFIX)-$(CLUSTER_SUFFIX)"' $(KWOK_DIR)/$(CONFIG) > $(KWOK_DIR)/$(CONFIG).tmp && \
		mv $(KWOK_DIR)/$(CONFIG).tmp $(KWOK_DIR)/$(CONFIG); \
	fi
	@SIMULATOR_ARGS="--config $(CONFIG) --num-clusters $(NUM_CLUSTERS) --start-index $(START_INDEX) --num-nodes $(NUM_NODES)"; \
	if [ -n "$(MAX_WORKERS)" ]; then \
		SIMULATOR_ARGS="$$SIMULATOR_ARGS --max-workers $(MAX_WORKERS)"; \
	fi; \
	if [ "$(NO_PARALLEL)" = "1" ]; then \
		SIMULATOR_ARGS="$$SIMULATOR_ARGS --no-parallel"; \
	fi; \
	cd $(KWOK_DIR) && . venv/bin/activate && ./kwok_cluster_simulator.py $$SIMULATOR_ARGS

cleanup-cluster: setup-python-if-needed
	@if [ -z "$(CLUSTER)" ]; then \
		echo "Error: CLUSTER variable required."; \
		echo "Usage: make cleanup-cluster CLUSTER=zks-scale-test-200"; \
		exit 1; \
	fi
	@echo "Cleaning up cluster: $(CLUSTER)"
	cd $(KWOK_DIR) && . venv/bin/activate && ./kwok_cluster_simulator.py --config $(CONFIG) --cleanup $(CLUSTER)

cleanup: setup-python-if-needed
	@echo "Cleaning up all simulated clusters..."
	@SIMULATOR_ARGS="--config $(CONFIG) --cleanup-all"; \
	if [ -n "$(MAX_WORKERS)" ]; then \
		SIMULATOR_ARGS="$$SIMULATOR_ARGS --max-workers $(MAX_WORKERS)"; \
	fi; \
	if [ "$(NO_PARALLEL)" = "1" ]; then \
		SIMULATOR_ARGS="$$SIMULATOR_ARGS --no-parallel"; \
	fi; \
	cd $(KWOK_DIR) && . venv/bin/activate && ./kwok_cluster_simulator.py $$SIMULATOR_ARGS
