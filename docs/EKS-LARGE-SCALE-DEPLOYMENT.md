# EKS Large-Scale Deployment Guide (2500-3000 Nodes)

This guide explains how to deploy the ZKS Cluster Scale Simulator on Amazon EKS to simulate 2500-3000 nodes across multiple clusters.

## Overview

For large-scale deployments (2500-3000 nodes), you'll need:
1. **EKS Cluster** - To host agent pods (one pod per simulated cluster)
2. **EC2 Instance** - To run the KWOK container (or run KWOK as a pod in EKS)
3. **Proper Resource Planning** - Memory, CPU, and network considerations

## Architecture for Large Scale

```
┌─────────────────────────────────────────────────────────────┐
│                    EKS Cluster                                │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Agent Pods (one per simulated cluster)              │   │
│  │  - zks-agent-sim-1 (cluster-1)                       │   │
│  │  - zks-agent-sim-2 (cluster-2)                       │   │
│  │  - ... (up to 2500-3000 pods)                        │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                        │
                        │ WebSocket Connections
                        │
┌───────────────────────┴───────────────────────────────────────┐
│              ZKS Server                                        │
└───────────────────────────────────────────────────────────────┘
                        │
                        │ API Requests
                        │
┌───────────────────────┴───────────────────────────────────────┐
│         KWOK Container (on EC2 or EKS Pod)                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Shared KWOK API Server                              │   │
│  │  - Simulated Nodes: 2500-3000                         │   │
│  │  - Organized by cluster labels                       │   │
│  └──────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────┘
```

## Resource Planning

### Scenario: 2500-3000 Nodes

**Option 1: Distributed Across Multiple Clusters**
- **250 clusters × 10 nodes each = 2500 nodes**
- **300 clusters × 10 nodes each = 3000 nodes**
- **500 clusters × 5 nodes each = 2500 nodes**

**Option 2: Fewer Large Clusters**
- **50 clusters × 50 nodes each = 2500 nodes**
- **30 clusters × 100 nodes each = 3000 nodes**

### Resource Requirements

#### EKS Cluster Sizing

**For 250-300 Agent Pods:**
- **Node Instance Type**: `m5.xlarge` (4 vCPU, 16 GB RAM) or `m5.2xlarge` (8 vCPU, 32 GB RAM)
- **Number of Nodes**: 10-15 nodes (depending on instance size)
- **Pods per Node**: ~20-30 agent pods per node
- **Total Pods**: 250-300 agent pods + system pods

**Calculation:**
- Each agent pod: ~50-100 MB RAM
- 300 pods × 100 MB = ~30 GB RAM
- With overhead: ~40-50 GB total RAM needed
- 10 nodes × 16 GB = 160 GB (comfortable margin)

#### KWOK Container Resources

**For 2500-3000 Simulated Nodes:**
- **Memory**: 2-4 GB RAM (KWOK container)
- **CPU**: 2-4 vCPU
- **Storage**: Minimal (etcd data)

**KWOK Container Configuration:**
```json
{
  "kwok_memory_limit": "4Gi",
  "kwok_gogc": 50,
  "kwok_gomemlimit": "2GiB",
  "kwok_etcd_quota": "1Gi"
}
```

#### EC2 Instance for KWOK (if not running in EKS)

**Recommended Instance:**
- **Instance Type**: `m5.xlarge` (4 vCPU, 16 GB RAM) or `m5.2xlarge` (8 vCPU, 32 GB RAM)
- **Storage**: 50 GB SSD
- **Network**: Enhanced networking enabled

## Step-by-Step Setup

### Step 1: Create EKS Cluster

#### Using AWS CLI

```bash
# Set variables
export CLUSTER_NAME=zks-simulator-eks
export REGION=us-east-1
export NODE_GROUP_NAME=simulator-nodes
export NODE_INSTANCE_TYPE=m5.xlarge
export MIN_NODES=5
export MAX_NODES=20
export DESIRED_NODES=10

# Create EKS cluster
eksctl create cluster \
  --name $CLUSTER_NAME \
  --region $REGION \
  --nodegroup-name $NODE_GROUP_NAME \
  --node-type $NODE_INSTANCE_TYPE \
  --nodes $DESIRED_NODES \
  --nodes-min $MIN_NODES \
  --nodes-max $MAX_NODES \
  --managed \
  --with-oidc \
  --ssh-access

# Update kubeconfig
aws eks update-kubeconfig --name $CLUSTER_NAME --region $REGION
```

#### Using Terraform (Alternative)

Create `eks-cluster.tf`:

```hcl
provider "aws" {
  region = "us-east-1"
}

module "eks" {
  source = "terraform-aws-modules/eks/aws"
  version = "~> 19.0"

  cluster_name    = "zks-simulator-eks"
  cluster_version = "1.28"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Node group configuration
  eks_managed_node_groups = {
    simulator = {
      min_size     = 5
      max_size     = 20
      desired_size = 10

      instance_types = ["m5.xlarge"]
      capacity_type  = "ON_DEMAND"

      # Increase pod density
      create_launch_template = true
      launch_template_name   = "simulator-nodes"
      
      # Allow more pods per node
      kubelet_extra_args = "--max-pods=110"
    }
  }

  # Enable cluster autoscaling
  cluster_addons = {
    kube-proxy = {}
    vpc-cni    = {}
    coredns = {
      most_recent = true
    }
  }
}

module "vpc" {
  source = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "zks-simulator-vpc"
  cidr = "10.0.0.0/16"

  azs             = ["us-east-1a", "us-east-1b", "us-east-1c"]
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]

  enable_nat_gateway = true
  enable_vpn_gateway = false
}
```

### Step 2: Create EC2 Instance for KWOK (Optional)

If running KWOK on a separate EC2 instance:

```bash
# Launch EC2 instance
aws ec2 run-instances \
  --image-id ami-0c55b159cbfafe1f0 \
  --instance-type m5.xlarge \
  --key-name your-key-name \
  --security-group-ids sg-xxxxx \
  --subnet-id subnet-xxxxx \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=kwok-simulator}]'
```

**Security Group Rules:**
- **Inbound**: Port 8050 from EKS nodes (or allow all from VPC)
- **Outbound**: All traffic

### Step 3: Install Simulator on EC2 Instance

```bash
# SSH to EC2 instance
ssh -i your-key.pem ec2-user@your-instance-ip

# Install Docker
sudo yum update -y
sudo yum install -y docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER

# Install kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# Configure EKS access
aws eks update-kubeconfig --name zks-simulator-eks --region us-east-1

# Clone and setup simulator
cd ~
git clone https://github.com/vivek-zededa/cluster-scale-simulator-main-v2.git
cd cluster-scale-simulator-main-v2/shared-kwok-simulator/kwok-simulator
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 4: Configure for Large Scale

Create `my-config.json`:

```json
{
  "import_mode": "zks",
  
  "zks_url": "https://your-zks-server.example.com",
  "zks_token": "token-xxxxx:your-token",
  "zks_kubeconfig_path": "/home/ec2-user/zks-kubeconfig.yaml",
  
  "host_kubeconfig": "/home/ec2-user/.kube/config",
  "host_context": "arn:aws:eks:us-east-1:xxxxx:cluster/zks-simulator-eks",
  
  "shared_kwok_cluster_name": "shared-kwok",
  "shared_kwok_api_port": 8050,
  "cluster_name_prefix": "kwok-scale-test",
  "kwok_base_port": 10000,
  "agent_image": "adib146/cluster-agent-simulator:shared-v64",
  "agent_namespace": "zks-simulator",
  "agent_poll_interval": 1800,
  "kwok_image": "ghcr.io/kwok-ci/cluster:v0.7.0-k8s.v1.34.1",
  
  "max_workers": 20,
  "kwok_memory_limit": "4Gi",
  "kwok_gogc": 50,
  "kwok_gomemlimit": "2GiB",
  "kwok_etcd_quota": "1Gi",
  
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

### Step 5: Create Namespace in EKS

```bash
kubectl create namespace zks-simulator
```

### Step 6: Run Simulator

#### Option A: 250 clusters with 10 nodes each (2500 nodes)

```bash
cd ~/cluster-scale-simulator-main-v2/shared-kwok-simulator/kwok-simulator
source venv/bin/activate

python3 kwok_cluster_simulator.py \
  --config my-config.json \
  --num-clusters 250 \
  --num-nodes 10 \
  --max-workers 20
```

#### Option B: 300 clusters with 10 nodes each (3000 nodes)

```bash
python3 kwok_cluster_simulator.py \
  --config my-config.json \
  --num-clusters 300 \
  --num-nodes 10 \
  --max-workers 20
```

#### Option C: 50 clusters with 50 nodes each (2500 nodes)

```bash
python3 kwok_cluster_simulator.py \
  --config my-config.json \
  --num-clusters 50 \
  --num-nodes 50 \
  --max-workers 20
```

## Alternative: Run KWOK as Pod in EKS

Instead of running KWOK on a separate EC2 instance, you can run it as a pod in EKS:

### Create KWOK Deployment

Create `kwok-deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shared-kwok
  namespace: zks-simulator
spec:
  replicas: 1
  selector:
    matchLabels:
      app: shared-kwok
  template:
    metadata:
      labels:
        app: shared-kwok
    spec:
      containers:
      - name: kwok
        image: ghcr.io/kwok-ci/cluster:v0.7.0-k8s.v1.34.1
        ports:
        - containerPort: 32764
          name: api
        resources:
          requests:
            memory: "2Gi"
            cpu: "2"
          limits:
            memory: "4Gi"
            cpu: "4"
        command:
        - /kwok
        - --kube-apiserver-insecure-port=0
        - --etcd-quota-backend-size=1Gi
        - --disable-qps-limits
        - --kube-admission=false
        - --heartbeat-factor=10
        - --extra-args
        - kube-controller-manager=controllers=serviceaccount-controller,serviceaccount-token-controller,namespace-controller,endpoints-controller,replicaset-controller,deployment-controller,daemonset-controller,job-controller,pod-garbage-collector-controller,garbage-collector-controller
        - --extra-args
        - kube-apiserver=watch-cache-sizes=nodes#0,replicasets.apps#0,daemonsets.apps#0,statefulsets.apps#0,jobs.batch#0,ingresses.networking.k8s.io#0,persistentvolumeclaims#0,persistentvolumes#0
        - --extra-args
        - kube-apiserver=feature-gates=OpenAPIEnums=false,APIServerIdentity=false,StorageVersionHash=false
        env:
        - name: GOGC
          value: "50"
        - name: GOMEMLIMIT
          value: "2GiB"
---
apiVersion: v1
kind: Service
metadata:
  name: shared-kwok
  namespace: zks-simulator
spec:
  selector:
    app: shared-kwok
  ports:
  - port: 8050
    targetPort: 32764
    protocol: TCP
  type: ClusterIP
```

Deploy:

```bash
kubectl apply -f kwok-deployment.yaml
```

Update config to use service:

```json
{
  "shared_kwok_api_port": 8050,
  "kwok_service_url": "shared-kwok.zks-simulator.svc.cluster.local:8050"
}
```

## Monitoring and Scaling

### Monitor Resource Usage

```bash
# Check node resources
kubectl top nodes

# Check pod resources
kubectl top pods -n zks-simulator

# Check KWOK container (if on EC2)
docker stats shared-kwok

# Check number of agent pods
kubectl get pods -n zks-simulator | wc -l
```

### Auto-Scaling EKS Nodes

EKS will automatically scale nodes based on pod scheduling needs:

```bash
# Check node group status
aws eks describe-nodegroup \
  --cluster-name zks-simulator-eks \
  --nodegroup-name simulator-nodes \
  --region us-east-1
```

### Monitor KWOK Performance

```bash
# Check KWOK logs
docker logs shared-kwok --tail 100

# Or if running as pod
kubectl logs -n zks-simulator deployment/shared-kwok --tail 100
```

## Performance Optimization

### For 2500-3000 Nodes

1. **Increase Parallel Workers**: Use `--max-workers 20-30` for faster creation
2. **Batch Creation**: Create clusters in batches to avoid API rate limits
3. **Resource Limits**: Ensure KWOK has sufficient memory (4GB+)
4. **Network Optimization**: Deploy close to ZKS server or use VPN
5. **EKS Node Sizing**: Use larger instances (m5.2xlarge) for better pod density

### Recommended Settings

```json
{
  "max_workers": 20,
  "kwok_memory_limit": "4Gi",
  "kwok_etcd_quota": "1Gi",
  "agent_poll_interval": 1800
}
```

## Troubleshooting

### Issue: Pods Not Scheduling

**Solution:**
- Check node capacity: `kubectl describe nodes`
- Increase node count or instance size
- Check pod resource requests

### Issue: KWOK Container Out of Memory

**Solution:**
- Increase `kwok_memory_limit` to 4-8GB
- Reduce `kwok_etcd_quota` if not needed
- Monitor with `docker stats`

### Issue: Slow Cluster Creation

**Solution:**
- Increase `max_workers` (but watch API rate limits)
- Check network latency to ZKS server
- Verify EKS nodes have sufficient resources

### Issue: API Rate Limiting

**Solution:**
- Reduce `max_workers` to 10-15
- Add delays between batches
- Contact ZKS team to increase rate limits

## Cost Estimation

### EKS Cluster (10 nodes, m5.xlarge)
- **Compute**: 10 × $0.192/hour = $1.92/hour = ~$1,382/month
- **EKS Control Plane**: $0.10/hour = ~$72/month
- **Total**: ~$1,454/month

### EC2 Instance (m5.xlarge for KWOK)
- **Compute**: $0.192/hour = ~$138/month

### Total Estimated Cost
- **~$1,600/month** for 2500-3000 node simulation

**Note**: Costs vary by region and can be reduced with:
- Spot instances for EKS nodes
- Reserved instances
- Right-sizing based on actual usage

## Best Practices

1. **Start Small**: Test with 10-50 clusters first
2. **Monitor Resources**: Watch CPU, memory, and network usage
3. **Gradual Scaling**: Increase cluster count gradually
4. **Cleanup Regularly**: Remove unused clusters to free resources
5. **Use Spot Instances**: For cost savings (with proper node groups)
6. **Monitor Costs**: Set up AWS Cost Alerts

## Example: Complete Workflow

```bash
# 1. Create EKS cluster
eksctl create cluster --name zks-simulator-eks --region us-east-1 \
  --nodegroup-name simulator --node-type m5.xlarge --nodes 10

# 2. Setup EC2 instance and install simulator
# (Follow Step 3 above)

# 3. Create 250 clusters with 10 nodes each
cd ~/cluster-scale-simulator-main-v2/shared-kwok-simulator/kwok-simulator
source venv/bin/activate

python3 kwok_cluster_simulator.py \
  --config my-config.json \
  --num-clusters 250 \
  --num-nodes 10 \
  --max-workers 20

# 4. Monitor
kubectl get pods -n zks-simulator
kubectl top nodes

# 5. Cleanup when done
python3 kwok_cluster_simulator.py --config my-config.json --cleanup-all
```

## Additional Resources

- [AWS EKS Documentation](https://docs.aws.amazon.com/eks/)
- [EKS Best Practices](https://aws.github.io/aws-eks-best-practices/)
- [KWOK Documentation](https://kwok.sigs.k8s.io/)
- [Getting Started Guide](GETTING-STARTED.md)
- [AWS Deployment Guide](AWS-DEPLOYMENT-GUIDE.md)
