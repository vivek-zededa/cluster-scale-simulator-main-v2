# AWS VM Deployment Guide

This guide provides step-by-step instructions for deploying and running the ZKS Cluster Scale Simulator on an AWS EC2 instance.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [AWS EC2 Instance Setup](#aws-ec2-instance-setup)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Network Configuration](#network-configuration)
6. [Running the Simulator](#running-the-simulator)
7. [Troubleshooting](#troubleshooting)
8. [Performance Optimization](#performance-optimization)

## Prerequisites

Before starting, ensure you have:

- **AWS Account** with EC2 access
- **SSH access** to your EC2 instance
- **ZKS Server** running and accessible from the EC2 instance
- **Kubernetes cluster** (EKS, self-managed, or k3d/kind) accessible from the EC2 instance

## AWS EC2 Instance Setup

### 1. Launch EC2 Instance

**Recommended Instance Types:**
- **Small scale (1-50 clusters)**: `t3.medium` (2 vCPU, 4 GB RAM)
- **Medium scale (50-200 clusters)**: `t3.large` (2 vCPU, 8 GB RAM) or `t3.xlarge` (4 vCPU, 16 GB RAM)
- **Large scale (200+ clusters)**: `t3.2xlarge` (8 vCPU, 32 GB RAM) or `m5.xlarge` (4 vCPU, 16 GB RAM)

**Instance Configuration:**
- **AMI**: Amazon Linux 2023, Ubuntu 22.04 LTS, or similar
- **Storage**: Minimum 20 GB (SSD recommended)
- **Security Group**: See [Network Configuration](#network-configuration) section

### 2. Connect to Instance

```bash
ssh -i your-key.pem ec2-user@your-instance-ip
# or for Ubuntu:
ssh -i your-key.pem ubuntu@your-instance-ip
```

## Installation

### Step 1: Install Docker

**For Amazon Linux 2023:**
```bash
sudo yum update -y
sudo yum install -y docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER
# Log out and back in for group changes to take effect
```

**For Ubuntu 22.04:**
```bash
sudo apt-get update
sudo apt-get install -y docker.io
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER
# Log out and back in for group changes to take effect
```

**Verify Docker installation:**
```bash
docker --version
docker ps
```

### Step 2: Install Kubernetes Tools

**Install kubectl:**
```bash
# For Amazon Linux / RHEL
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
kubectl version --client

# For Ubuntu
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
kubectl version --client
```

**Install k3d (if using local k3d cluster):**
```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
```

### Step 3: Install Python and Dependencies

**For Amazon Linux 2023:**
```bash
sudo yum install -y python3 python3-pip git
```

**For Ubuntu 22.04:**
```bash
sudo apt-get install -y python3 python3-pip python3-venv git
```

### Step 4: Clone and Setup Simulator

```bash
# Clone the repository (or upload files)
cd ~
git clone <repository-url> cluster-scale-simulator
cd cluster-scale-simulator

# Setup Python virtual environment
cd shared-kwok-simulator/kwok-simulator
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

### Step 1: Create Configuration File

```bash
cd ~/cluster-scale-simulator/shared-kwok-simulator/kwok-simulator
cp config-template.json my-config.json
```

### Step 2: Edit Configuration

Edit `my-config.json` with your values:

```json
{
  "import_mode": "zks",
  
  "zks_url": "https://your-zks-server.example.com",
  "zks_token": "token-xxxxx:your-token",
  "zks_kubeconfig_path": "/home/ec2-user/zks-kubeconfig.yaml",
  
  "host_kubeconfig": "/home/ec2-user/.kube/config",
  "host_context": "your-k8s-context",
  
  "shared_kwok_cluster_name": "shared-kwok",
  "shared_kwok_api_port": 8050,
  "cluster_name_prefix": "kwok-scale-test",
  "kwok_base_port": 10000,
  "agent_image": "adib146/cluster-agent-simulator:shared-v64",
  "agent_namespace": "zks-simulator",
  "agent_poll_interval": 1800,
  "kwok_image": "ghcr.io/kwok-ci/cluster:v0.7.0-k8s.v1.34.1",
  
  "max_workers": 10,
  "kwok_memory_limit": "512m",
  "kwok_gogc": 50,
  "kwok_gomemlimit": "150MiB",
  "kwok_etcd_quota": "256Mi",
  
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

**Key AWS-specific settings:**
- `max_workers`: Set to 10-20 for parallel execution (adjust based on instance size)
- `kwok_memory_limit`: Limit KWOK container memory (e.g., "512m" for small instances)
- `host_context`: Your Kubernetes context name (check with `kubectl config get-contexts`)

### Step 3: Configure Kubernetes Access

**For EKS:**
```bash
# Install AWS CLI if not already installed
sudo yum install -y aws-cli  # Amazon Linux
# or
sudo apt-get install -y awscli  # Ubuntu

# Configure AWS credentials
aws configure

# Get EKS kubeconfig
aws eks update-kubeconfig --region us-east-1 --name your-cluster-name
```

**For k3d (local cluster on EC2):**
```bash
# Create a local k3d cluster
k3d cluster create mycluster --port "8080:80@loadbalancer"

# Get kubeconfig
k3d kubeconfig get mycluster > ~/.kube/config
```

**Verify Kubernetes access:**
```bash
kubectl get nodes
kubectl config get-contexts
```

### Step 4: Download ZKS Kubeconfig

Download your ZKS kubeconfig and save it:
```bash
# Save ZKS kubeconfig
nano ~/zks-kubeconfig.yaml
# Paste your ZKS kubeconfig content
```

## Network Configuration

### Security Group Rules

Configure your EC2 security group to allow:

**Inbound Rules:**
- **SSH (22)**: From your IP address
- **Custom TCP (8050)**: From your Kubernetes cluster (if needed)
- **HTTPS (443)**: Outbound to ZKS server (already allowed by default)

**Outbound Rules:**
- **All traffic**: To 0.0.0.0/0 (default)

### Docker Network Configuration

The simulator automatically detects network configuration. For AWS VMs:

1. **EKS Clusters**: The simulator will use bridge network IPs
2. **Local k3d/kind**: The simulator will connect to Docker networks automatically
3. **Remote Clusters**: Ensure pods can reach the EC2 instance's private IP

**For EKS, ensure:**
- EC2 instance and EKS cluster are in the same VPC or have VPC peering
- Security groups allow traffic between EC2 and EKS nodes

## Running the Simulator

### Basic Usage

```bash
cd ~/cluster-scale-simulator/shared-kwok-simulator/kwok-simulator
source venv/bin/activate

# Create 5 clusters with 2 nodes each
python3 kwok_cluster_simulator.py --config my-config.json \
  --num-clusters 5 \
  --num-nodes 2

# Create with parallel execution (faster)
python3 kwok_cluster_simulator.py --config my-config.json \
  --num-clusters 10 \
  --num-nodes 2 \
  --max-workers 10
```

### Using Makefile

```bash
cd ~/cluster-scale-simulator

# Create clusters
make run-simulator NUM_CLUSTERS=10 NUM_NODES=2

# Cleanup all clusters
make cleanup

# Cleanup specific cluster
make cleanup-cluster CLUSTER=kwok-scale-test-1
```

### Parallel Execution

The simulator supports parallel execution for faster cluster creation/cleanup:

```bash
# Enable parallel execution (default)
python3 kwok_cluster_simulator.py --config my-config.json \
  --num-clusters 50 \
  --max-workers 10

# Disable parallel execution (sequential)
python3 kwok_cluster_simulator.py --config my-config.json \
  --num-clusters 50 \
  --no-parallel
```

**Recommended `max_workers` values:**
- Small instances (t3.medium): 5-10
- Medium instances (t3.large/xlarge): 10-15
- Large instances (t3.2xlarge+): 15-20

## Troubleshooting

### Issue: Docker Permission Denied

**Solution:**
```bash
sudo usermod -aG docker $USER
# Log out and back in
newgrp docker
```

### Issue: Cannot Connect to Kubernetes Cluster

**Solution:**
```bash
# Verify kubeconfig
kubectl config get-contexts
kubectl config use-context your-context-name
kubectl get nodes

# For EKS, refresh kubeconfig
aws eks update-kubeconfig --region us-east-1 --name your-cluster-name
```

### Issue: KWOK Container Cannot Reach Pods

**Solution:**
- Check that EC2 instance and Kubernetes nodes are in the same network
- Verify security group rules allow traffic
- For EKS, ensure VPC configuration is correct

### Issue: Out of Memory

**Solution:**
- Reduce `max_workers` in config
- Set `kwok_memory_limit` to a lower value (e.g., "256m")
- Reduce `kwok_etcd_quota` (e.g., "128Mi")
- Use a larger instance type

### Issue: Slow Cluster Creation

**Solution:**
- Enable parallel execution: `--max-workers 10`
- Check network latency to ZKS server
- Verify instance has sufficient CPU/memory
- Consider using a larger instance type

### Issue: Agent Pods Not Starting

**Solution:**
```bash
# Check pod status
kubectl get pods -n zks-simulator

# Check pod logs
kubectl logs -n zks-simulator -l app=zks-agent-sim

# Check if KWOK container is running
docker ps | grep shared-kwok

# Check KWOK logs
docker logs shared-kwok
```

## Performance Optimization

### Resource Limits

For lightweight operation on AWS:

```json
{
  "kwok_memory_limit": "256m",
  "kwok_gogc": 50,
  "kwok_gomemlimit": "100MiB",
  "kwok_etcd_quota": "128Mi",
  "max_workers": 10
}
```

### Scaling Recommendations

**Instance Sizing:**
- **1-50 clusters**: t3.medium (2 vCPU, 4 GB)
- **50-200 clusters**: t3.large (2 vCPU, 8 GB)
- **200-500 clusters**: t3.xlarge (4 vCPU, 16 GB)
- **500+ clusters**: t3.2xlarge (8 vCPU, 32 GB) or larger

**Memory Usage:**
- Shared KWOK container: ~200-500 MB
- Each agent pod: ~50-100 MB
- Total for 100 clusters: ~5-10 GB

### Best Practices

1. **Start Small**: Test with 1-5 clusters first
2. **Monitor Resources**: Use `htop` or `docker stats` to monitor usage
3. **Parallel Execution**: Use `--max-workers` for faster operations
4. **Cleanup Regularly**: Run `make cleanup` to free resources
5. **Network Optimization**: Ensure low latency to ZKS server

## Example: Full Workflow

```bash
# 1. Setup
cd ~/cluster-scale-simulator/shared-kwok-simulator/kwok-simulator
source venv/bin/activate

# 2. Create 10 clusters
python3 kwok_cluster_simulator.py --config my-config.json \
  --num-clusters 10 \
  --num-nodes 3 \
  --max-workers 10

# 3. Verify clusters
kubectl get pods -n zks-simulator
docker ps | grep shared-kwok

# 4. Cleanup when done
python3 kwok_cluster_simulator.py --config my-config.json --cleanup-all
```

## Additional Resources

- [Getting Started Guide](GETTING-STARTED.md) - General setup and usage
- [Architecture Documentation](ARCHITECTURE.md) - Technical details
- [KWOK Documentation](https://kwok.sigs.k8s.io/) - KWOK project details

## Support

For issues or questions:
1. Check the [Troubleshooting](#troubleshooting) section
2. Review logs: `docker logs shared-kwok` and `kubectl logs -n zks-simulator`
3. Verify network connectivity and security group rules
