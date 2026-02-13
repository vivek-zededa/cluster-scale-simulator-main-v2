# ZKS Cluster Scale Simulator

Simulate hundreds of Kubernetes clusters connected to ZKS Server for scale testing. Uses KWOK (Kubernetes WithOut Kubelet) for lightweight cluster simulation and custom agent simulators to mimic real zks-server-agent and zks-fleet-agent behavior.

## Features

- **Parallel Execution**: Create and cleanup clusters in parallel for faster operations
- **AWS VM Compatible**: Optimized for deployment on AWS EC2 instances
- **Lightweight**: Minimal resource usage with optimized KWOK settings
- **Scalable**: Support for hundreds of simulated clusters with a single shared KWOK container

## Documentation

- **[Getting Started Guide](docs/GETTING-STARTED.md)** - Setup, configuration, and running the simulator
- **[Architecture Documentation](docs/ARCHITECTURE.md)** - Technical details, agent communication, and capacity planning
- **[AWS Deployment Guide](docs/AWS-DEPLOYMENT-GUIDE.md)** - Step-by-step guide for deploying on AWS EC2 instances