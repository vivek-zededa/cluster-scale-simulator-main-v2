# ZKS Scale Simulator Architecture

High-level architecture for simulating hundreds of Kubernetes clusters connected to ZKS Server using KWOK and agent simulators.

## Overview

The ZKS Scale Simulator creates lightweight simulated Kubernetes clusters using a **shared KWOK architecture**. Instead of running separate KWOK containers per cluster, it uses **ONE shared KWOK container** that hosts multiple "logical clusters" separated by namespaces and labels. Each logical cluster gets its own dedicated agent simulator pod that connects to ZKS Server, making each cluster appear as a fully functional, independent cluster in ZKS Server.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           ZKS Cluster                                   │
│  ┌──────────────────┐  ┌────────────────┐                               │
│  │   ZKS Server     │  │   ZKS Fleet    │                               │
│  │   Controller     │  │   Controller   │                               │
│  └──────────────────┘  └────────────────┘                               │
└──────────────┬──────────────┬────────────────────────────────────────── ┘
               │              │
               │    WebSocket Connections (one per cluster)
               │              │
┌──────────────┴──────────────┴──────────────────────────────────────────┐
│                       Host Kubernetes Cluster                          │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  Namespace: zks-simulator                                       │   │
│  │                                                                 │   │
│  │  ┌─────────────┐  ┌─────────────┐       ┌─────────────┐         │   │
│  │  │ Agent Pod 1 │  │ Agent Pod 2 │  ...  │ Agent Pod N │         │   │
│  │  │ cluster-1   │  │ cluster-2   │       │ cluster-N   │         │   │
│  │  │ steve:9443  │  │ steve:9444  │       │ steve:9XXX  │         │   │
│  │  └──────┬──────┘  └──────┬──────┘       └──────┬──────┘         │   │
│  │         │                 │                     │               │   │
│  │         └─────────────────┴─────────────────────┘               │   │
│  │                           │                                     │   │
│  │  ┌────────────────────────▼──────────────────────────────────┐  │   │
│  │  │  ConfigMaps: Per-cluster kubeconfigs, CA certificates    │  │   │
│  │  └────────────────────────────────────────────────────────── ┘  │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└───────────────────────┬────────────────────────────────────────────────┘
                        │ All agents connect to same KWOK
                        │ (via host.docker.internal or k3d/kind network)
                        │
┌───────────────────────┴────────────────────────────────────────────────┐
│                    Host Machine (Docker)                               │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │            SHARED KWOK Container (port 8050)                     │  │
│  │            Single API Server + etcd                              │  │
│  │  ┌────────────────────────────────────────────────────────────┐ │  │
│  │  │ Logical Cluster 1 (namespace: kwok-scale-test-1)           │ │  │
│  │  │  ├─ Node: kwok-scale-test-1-node-001                       │ │  │
│  │  │  │        labels: cluster-id=kwok-scale-test-1             │ │  │
│  │  │  ├─ Node: kwok-scale-test-1-node-002                       │ │  │
│  │  │  │        labels: cluster-id=kwok-scale-test-1             │ │  │
│  │  │  └─ Node: kwok-scale-test-1-node-00N                       │ │  │
│  │  │           labels: cluster-id=kwok-scale-test-1             │ │  │
│  │  └────────────────────────────────────────────────────────────┘ │  │
│  │  ┌────────────────────────────────────────────────────────────┐ │  │
│  │  │ Logical Cluster 2 (namespace: kwok-scale-test-2)           │ │  │
│  │  │  ├─ Node: kwok-scale-test-2-node-001                       │ │  │
│  │  │  │        labels: cluster-id=kwok-scale-test-2             │ │  │
│  │  │  └─ Node: kwok-scale-test-2-node-00N                       │ │  │
│  │  │           labels: cluster-id=kwok-scale-test-2             │ │  │
│  │  └────────────────────────────────────────────────────────────┘ │  │
│  │  ┌────────────────────────────────────────────────────────────┐ │  │
│  │  │ Logical Cluster N (namespace: kwok-scale-test-N)           │ │  │
│  │  │  ├─ Node: kwok-scale-test-N-node-001                       │ │  │
│  │  │  │        labels: cluster-id=kwok-scale-test-N             │ │  │
│  │  │  └─ Node: kwok-scale-test-N-node-00N                       │ │  │
│  │  │           labels: cluster-id=kwok-scale-test-N             │ │  │
│  │  └────────────────────────────────────────────────────────────┘ │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. Shared KWOK Container (Simulated Kubernetes)

**Purpose**: Single lightweight Kubernetes API server serving multiple logical clusters

**Architecture**:
- **ONE Docker container** running KWOK (Kubernetes WithOut Kubelet)
- **ONE API server** + **ONE etcd** instance (shared by all logical clusters)
- Runs on port 8050 (configurable)
- Connected to host K8s network for pod access

**Logical Cluster Separation**:
- Each logical cluster has a **dedicated namespace** created in shared KWOK (for potential future use)
- **Nodes are cluster-scoped** (not in namespaces) with **unique names and labels**
- Each node labeled with `cluster-id=<cluster-name>` (e.g., `cluster-id=kwok-scale-test-1`)
- Example node names: `kwok-scale-test-1-node-001`, `kwok-scale-test-2-node-001`
- Each logical cluster gets a **dedicated kubeconfig** (same API endpoint, different cluster names)
- **Isolation achieved purely through label-based filtering** in the agent
- Appears as completely independent clusters to ZKS

**What's Real**: Single API server, single etcd (shared)
**What's Fake**: Nodes, pods, controllers
**What's Isolated**: Namespaces, node labels, kubeconfigs

### 2. ZKS Agent Simulator Pods (Go Binary)

**Purpose**: Each logical cluster gets its own dedicated agent pod that simulates cattle-agent and fleet-agent

**Critical**: While the KWOK API server is shared, **each logical cluster has its own agent pod** running in the host K8s cluster. This maintains independent WebSocket connections and cluster identity.

**How it Works**:

1. **Cattle-Agent Tunnel** (WebSocket Connection):
   - Connects to `wss://rancher-server/v3/connect/register` initially (Rancher API endpoint)
   - Uses **unique clientKey per cluster** to prevent tunnel pooling
   - Establishes remotedialer tunnel for bi-directional communication
   - Proxies ZKS API requests to shared KWOK container
   - Polls `/v3/connect/config` periodically for plan updates (configurable interval)
   - Serves steve aggregation endpoint on **unique port per cluster** (9443, 9444, 9445...)

2. **Steve Client Connection** (Separate WebSocket):
   - Connects using `stv-cluster-<token>` client key
   - Handles `/ping` health checks from clusterconnected controller
   - Uses TLS certificates **signed by KWOK CA** (critical for Rancher validation)
   - Responds to health checks with HTTP 200 OK

3. **Request Filtering & Isolation** (Critical for Multi-tenancy):
   - **Injects `labelSelector=cluster-id=<cluster-name>`** into all node queries
   - Example: For cluster "kwok-scale-test-1", injects `labelSelector=cluster-id=kwok-scale-test-1`
   - KWOK API filters and returns only nodes with matching label
   - **Blocks watch requests** for nodes (prevents cross-cluster leakage)
   - Strips pagination parameters to ensure all cluster nodes are returned
   - Each agent only sees its own logical cluster's resources

4. **Fleet Agent Status Reporter**:
   - Updates `clusters.fleet.cattle.io` status periodically (configurable interval)
   - Sets `status.agent.lastSeen` timestamp
   - Patches resource via ZKS API with bearer token authentication

5. **BundleDeployment Status Updater**:
   - Monitors all BundleDeployments for the cluster
   - Updates status to Ready with matching `appliedDeploymentID`
   - Reports resource counts (ready, modified, missing, etc.)
   - Runs periodically to catch GitRepo deployments

**Key Features**:
- **Dedicated agent pod per logical cluster** (not shared!)
- Dual WebSocket tunnels (cattle-agent + steve client)
- In-memory TLS certificates signed by KWOK CA
- **Smart request filtering** with labelSelector injection
- **Watch request blocking** to prevent data leakage
- Periodic status updates via REST API
- **Unique steve port allocation** per cluster

### 3. Host Kubernetes Cluster (Agent Pod Deployment)

**Purpose**: Runs dedicated zks-agent-sim pods (one per logical cluster) with controlled resource allocation

**Deployment Strategy**:
- **Each logical cluster gets its own agent pod** (Deployment with replicas=1)
- Namespace: `zks-simulator`
- Pods connect to shared KWOK container via:
  - `host.docker.internal` (Docker Desktop)
  - Direct network IP (k3d, kind - auto-detected)
- Each pod gets **unique steve port** (9443, 9444, 9445...)
- ConfigMaps store per-cluster KWOK kubeconfig and CA certificates
- Secret stores ZKS kubeconfig for Fleet API access

**Pod Configuration**:
- Configurable resource requests and limits
- Volumes: Cluster kubeconfig (ConfigMap), CA certs (ConfigMap), ZKS kubeconfig (Secret)
- Environment variables: CLUSTER_ID, POLL_INTERVAL, STEVE_PORT

**Resource Allocation (per agent pod)**:
- Typical: ~50MB RAM per agent pod
- Scales linearly: 100 clusters = 100 agent pods (~5GB RAM)
- Much lighter than running 100 separate KWOK containers (~20GB RAM)

### 4. Python Automation (kwok_cluster_simulator.py)

**Purpose**: Orchestrates creation, registration, and cleanup of logical clusters in shared KWOK

**Key Functions**:
- **Initializes shared KWOK container** (one-time, reused by all logical clusters)
- **Creates namespaces** in shared KWOK for each logical cluster (for organization)
- **Creates cluster-scoped nodes** with unique names and `cluster-id` labels for isolation
  - Node naming: `<cluster-name>-node-001`, `<cluster-name>-node-002`, etc.
  - Label: `cluster-id=<cluster-name>` (e.g., `cluster-id=kwok-scale-test-1`)
- Generates **per-cluster kubeconfigs** pointing to shared KWOK API
- Imports clusters to ZKS via `/v3/clusters` API (Rancher API endpoint)
- Retrieves registration tokens using cluster-specific endpoint
- **Deploys dedicated agent pod per cluster** with unique steve port
- Manages cleanup of namespaces, nodes, agent pods, and ZKS cluster resources

**Shared KWOK Initialization**:
- Creates single Docker container with KWOK all-in-one image
- Connects to host K8s network (auto-detects k3d/kind/Docker Desktop)
- Extracts CA certificates for agent pod TLS signing
- Stores base kubeconfig for namespace-scoped generation

## Agent Communication Flow

**Initial Registration**:
1. Agent pod connects to `wss://rancher/v3/connect/register` (Rancher API endpoint)
2. Creates `stv-aggregation` secret with steve token
3. Establishes cattle-agent WebSocket tunnel with **unique clientKey** (prevents pooling)

**Ongoing Operations**:
1. **Cattle-Agent Tunnel**: Handles API request proxying via remotedialer
2. **Steve Client**: Separate WebSocket for health checks (`/ping`)
3. **Status Updates**: Periodic PATCH requests to Fleet resources (configurable interval)
4. **Config Polling**: Calls `/v3/connect/config` periodically for plan updates (configurable interval)

**Request Proxying with Filtering**:
- ZKS Server → cattle-agent tunnel → 127.0.0.1:XXXX (unique steve port)
- **Steve handler intercepts node requests**:
  - Injects `labelSelector=cluster-id=X` to query parameters
  - Blocks watch requests (prevents showing all nodes)
  - Strips pagination to ensure complete results
- Steve handler → Shared KWOK API with client cert auth
- **KWOK filters by label** → returns only this cluster's nodes
- KWOK responds → steve → tunnel → ZKS Server

**Multi-Tenancy via Label Filtering**:
```
ZKS Query: GET /api/v1/nodes
Agent Rewrites: GET /api/v1/nodes?labelSelector=cluster-id=kwok-scale-test-1
KWOK Returns: Only nodes with cluster-id=kwok-scale-test-1 label
```
