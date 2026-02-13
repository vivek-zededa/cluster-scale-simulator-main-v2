# Getting Started with ZKS Scale Simulator

This guide walks you through setting up and running the ZKS Scale Simulator to test ZKS Server at scale.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Configuration](#configuration)
3. [Running the Simulator](#running-the-simulator)
4. [Verification](#verification)
5. [Cleanup](#cleanup)

## Prerequisites

Before you begin, ensure you have:

- **Docker**: Installed and running
- **Kubernetes cluster**: Docker Desktop, k3d, kind, or cloud cluster (AKS/GKE/EKS)
- **Python 3.8+**: For orchestration scripts
- **ZKS Server**: Running instance with API access

**Note:** Go is only needed if building the agent from source. Pre-built images are available.

## Configuration

### 1. Create Configuration File

Copy the example configuration:

```bash
cp shared-kwok-simulator/kwok-simulator/config-template.json shared-kwok-simulator/kwok-simulator/my-config.json
```

### 2. Edit Configuration

Open `shared-kwok-simulator/kwok-simulator/my-config.json` and update with your values:

```json
{
  "zks_url": "https://zks.example.com",
  "zks_token": "token-xxxxx:your-zks-api-token",
  "zks_kubeconfig_path": "/path/to/zks-kubeconfig.yaml",
  "host_kubeconfig": "/Users/youruser/.kube/config",
  "host_context": "docker-desktop",
  
  "shared_kwok_cluster_name": "shared-kwok",
  "shared_kwok_api_port": 8050,
  "cluster_name_prefix": "zks-scale-test",
  "kwok_base_port": 10000,
  "agent_image": "adib146/cluster-agent-simulator:shared-v57",
  "agent_namespace": "zks-simulator",
  "agent_poll_interval": 1800,
  "kwok_image": "ghcr.io/kwok-ci/cluster:v0.7.0-k8s.v1.34.1",
  
  "node_capacity": {
    "cpu": "4",
    "memory": "8Gi",
    "pods": "110"
  },
  "node_allocatable": {
    "cpu": "3800m",
    "memory": "7Gi",
    "pods": "110"
  }
}
```

### Configuration Fields Explained

#### Required Fields

- **`zks_url`**: Your ZKS server URL (e.g., `https://zks.example.com`)
- **`zks_token`**: ZKS API token - create via ZKS UI: **User Menu → Account & API Keys → Create API Key**
- **`zks_kubeconfig_path`**: Path to ZKS kubeconfig - download via ZKS UI: **Cluster → ⋮ Menu → Download KubeConfig**
- **`host_kubeconfig`**: Path to your local Kubernetes cluster kubeconfig (usually `~/.kube/config`)
- **`host_context`**: Context name in host kubeconfig - run `kubectl config get-contexts` to see available contexts

#### Optional Fields

- **`shared_kwok_cluster_name`**: Name of the shared KWOK Docker container (default: `shared-kwok`)
- **`shared_kwok_api_port`**: Port for shared KWOK API server (default: `8050`)
- **`cluster_name_prefix`**: Prefix for cluster names (default: `zks-scale-test`)
- **`kwok_base_port`**: Starting port for internal routing (default: `10000`) - not exposed to host
- **`agent_image`**: Agent simulator Docker image (default: `adib146/cluster-agent-simulator:shared-v85`)
- **`agent_namespace`**: Kubernetes namespace for agent pods (default: `zks-simulator`)
- **`agent_poll_interval`**: Agent polling interval in seconds for status updates and config checks (default: `1800` = 30 minutes)
- **`kwok_image`**: KWOK Docker image (default: `ghcr.io/kwok-ci/cluster:v0.7.0-k8s.v1.34.1`)
- **`node_capacity`**: Resource capacity for simulated nodes (CPU, memory, pods)
- **`node_allocatable`**: Allocatable resources on simulated nodes

**Cluster Naming:** `{prefix}-{number}` (e.g., `zks-scale-test-1`, `zks-scale-test-2`)

### Import Modes

The simulator supports two modes for importing clusters:

#### ZKS Direct Mode (default)

Imports clusters directly to a ZKS server. You specify which ZKS backend to use.

```json
{
  "import_mode": "zks",
  "zks_url": "https://zks.example.com",
  "zks_token": "token-xxxxx:your-token",
  "zks_kubeconfig_path": "/path/to/zks-kubeconfig.yaml"
}
```

#### Zedcloud Mode

Imports clusters via Zedcloud API. Zedcloud decides which ZKS backend to use based on the project.

```json
{
  "import_mode": "zedcloud",
  "zedcloud_url": "https://zedcontrol.example.com",
  "zedcloud_token": "username:secret",
  "zedcloud_project_name": "your-project",
  "zks_url": "https://zks-backend.example.com",
  "zks_kubeconfig_path": "/path/to/zks-kubeconfig.yaml"
}
```

**Note:** In Zedcloud mode, `zks_url` and `zks_kubeconfig_path` must match the ZKS backend that Zedcloud assigns to your project.

## Running the Simulator

## Running the Simulator

Start with a small number of clusters to verify your setup:

```bash
# Create 3 clusters with 2 nodes each
make run-simulator NUM_CLUSTERS=3 NUM_NODES=2
```

This command:
1. Builds the agent simulator Docker image
2. Creates a single shared KWOK container that simulates 3 logical clusters with 2 nodes each
3. Imports clusters into ZKS
4. Deploys 3 agent simulator pods (one per cluster) that connect to ZKS

**What gets created:**
- **Shared KWOK container**: ONE Docker container (`shared-kwok`) running on port 8050 that simulates multiple clusters
- **Agent pods**: Deployed in `zks-simulator` namespace on your Kubernetes cluster (one pod per simulated cluster)
- **ZKS clusters**: Appear in ZKS UI under "Cluster Management"

**Architecture:** The simulator uses a shared KWOK approach where one container hosts multiple logical clusters, each isolated by namespace and labels. This dramatically reduces resource usage compared to separate containers per cluster.

**Scaling up:**

```bash
# More clusters
make run-simulator NUM_CLUSTERS=10

# Resume adding clusters (if you already have 1-10, this creates 11-20)
make run-simulator NUM_CLUSTERS=10 START_INDEX=11

# Custom cluster naming
make run-simulator NUM_CLUSTERS=5 CLUSTER_PREFIX=prod CLUSTER_SUFFIX=test
```

**Note on START_INDEX:**
- Useful for incrementally adding clusters without recreating existing ones
- Example: Create clusters 1-5, then later add 6-10 using `START_INDEX=6`
- Cluster numbers: `{prefix}-{START_INDEX}` through `{prefix}-{START_INDEX + NUM_CLUSTERS - 1}`

## Verification

## Verification

Check that everything is running:

```bash
# View agent pods (one per simulated cluster)
kubectl get pods -n zks-simulator

# View shared KWOK container (should see ONE container)
docker ps | grep shared-kwok

# Check agent memory usage (should be ~60Mi per agent)
kubectl top pods -n zks-simulator
```

**In ZKS UI:**
1. Navigate to **Cluster Management**
2. Look for clusters named `kwok-scale-test-1`, `kwok-scale-test-2`, etc.
3. Each cluster should show **State: Active** (green)

**Check logs if cluster isn't connecting:**

```bash
kubectl logs -n zks-simulator -l cluster=kwok-scale-test-1
```

## Cleanup

Remove all simulated clusters:

```bash
make cleanup
```

This removes:
- All agent pods and deployments
- The shared KWOK Docker container
- Cluster registrations from ZKS

**Remove a specific cluster:**

```bash
make cleanup-cluster CLUSTER=zks-scale-test-200
```

**Manual cleanup if needed:**

```bash
# Remove agent pods
kubectl delete namespace zks-simulator

# Remove shared KWOK container
docker rm -f shared-kwok

# Or remove all kwok containers
docker ps -a | grep kwok | awk '{print $1}' | xargs docker rm -f
```

## Troubleshooting

### Fleet Agent Bundle Creation/Deletion Loop

**Symptom:** Fleet agent bundle for a cluster keeps getting created and deleted repeatedly (visible in ZKS UI or through bundle watch).

**Root Cause:** The agent's `agentDeployedGeneration` stays at 0, causing Fleet controller to think the agent isn't working and repeatedly delete/recreate the bundle to force a reset.

**Diagnosis:**

1. Find the cluster name from the bundle ID:
   ```bash
   # Replace c-xxxxx with your cluster ID
   kubectl --kubeconfig zks1-cert.yaml get clusters.management.cattle.io c-xxxxx -o jsonpath='{.spec.displayName}{"\n"}'
   ```

2. Check the agent pod logs:
   ```bash
   # Replace with the cluster name from step 1
   kubectl logs -n zks-simulator -l cluster-id=zks-scale-cluster-XXX --tail=100
   ```

   Look for:
   - `agentDeployedGeneration=0` in status updates
   - Reconciliation errors or timeouts
   - Connection issues to KWOK API

**Solution:**

Restart the agent pod to force re-registration and reconciliation:

```bash
# Delete the agent pod (Kubernetes will recreate it automatically)
kubectl delete pod -n zks-simulator -l cluster-id=zks-scale-cluster-XXX
```

**Expected behavior after restart:**
- Agent successfully connects and registers
- BundleDeployments reconcile
- `agentDeployedGeneration` increments from 0
- Bundle creation/deletion loop stops
- Cluster status becomes stable

**If the issue persists:**
- Check system resources (CPU/memory) - overload can cause timeout cascades
- Verify KWOK API server is responsive: `docker logs shared-kwok | tail -50`
- Check for related errors in ZKS server logs
