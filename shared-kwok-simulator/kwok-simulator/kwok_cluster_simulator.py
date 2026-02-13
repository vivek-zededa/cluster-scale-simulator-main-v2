#!/usr/bin/env python3
"""
Script to create KWOK clusters, import them to ZKS, and start agent simulators.
This script automates the complete workflow for scale testing with simulated clusters.
"""
# pylint: disable=logging-fstring-interpolation,too-many-locals,broad-exception-raised
import os
import sys
import json
import logging
import argparse
import subprocess
import time
import tempfile
from typing import Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from zedcloud_client import ZedcloudClient

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


class KwokClusterSimulator:
    """Handles KWOK cluster creation, import to ZKS, and agent simulation"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the simulator with configuration
        
        Args:
            config: Configuration dictionary containing:
                - zks_url: ZKS server URL
                - zks_token: Bearer token for authentication (optional if username/password provided)
                - zks_username: Username for authentication (optional if token provided)
                - zks_password: Password for authentication (optional if token provided)
                - zks_kubeconfig_path: Path to ZKS kubeconfig
                - host_kubeconfig: Path to host K8s cluster kubeconfig (for deploying agent pods)
                - host_context: Host K8s cluster context (default: docker-desktop)
                - cluster_name_prefix: Prefix for cluster names (default: kwok-scale-test)
                - kwok_image: KWOK all-in-one Docker image (default: ghcr.io/kwok-ci/cluster:v0.7.0-k8s.v1.34.1)
                - kwok_base_port: Starting port for KWOK clusters (default: 10000)
                - kwok_memory_limit: Docker memory limit (default: 200m)
                - kwok_gogc: GOGC garbage collection target percentage (default: 50)
                - kwok_gomemlimit: GOMEMLIMIT soft memory limit (default: 150MiB)
                - agent_image: Docker image for agent simulator (default: cluster-agent-simulator:latest)
                - agent_poll_interval: Agent polling interval in seconds (default: 60)
        """
        self.config = config
        
        # Import mode: "zks" (default) or "zedcloud"
        self.import_mode = config.get('import_mode', 'zks')
        logger.info(f"Import mode: {self.import_mode}")
        
        self.zks_url = config.get('zks_url', '').rstrip('/')
        self.cluster_name_prefix = config.get('cluster_name_prefix', 'kwok-scale-test')
        self.zks_kubeconfig_path = config.get('zks_kubeconfig_path', '')
        self.host_kubeconfig = config.get('host_kubeconfig', None)
        self.host_context = config.get('host_context', 'docker-desktop')
        self.kwok_image = config.get('kwok_image', 'ghcr.io/kwok-ci/cluster:v0.7.0-k8s.v1.34.1')
        self.kwok_base_port = config.get('kwok_base_port', 10000)
        self.port_counter = self.kwok_base_port
        self.cluster_info_dir = 'cluster-info'
        
        # Runtime optimization settings (optional - no defaults, let KWOK use what it needs)
        self.memory_limit = config.get('kwok_memory_limit')  # None = no limit
        self.gogc = config.get('kwok_gogc')  # None = Go default
        self.gomemlimit = config.get('kwok_gomemlimit')  # None = no soft limit
        
        # Agent simulator image
        self.agent_image = config.get('agent_image', 'adib146/cluster-agent-simulator:latest')
        
        # Agent polling interval (seconds)
        self.agent_poll_interval = config.get('agent_poll_interval', 60)
        
        # Parallel execution settings
        self.max_workers = config.get('max_workers', None)  # None = auto-detect based on CPU count
        if self.max_workers is None:
            import multiprocessing
            self.max_workers = min(multiprocessing.cpu_count(), 10)  # Cap at 10 to avoid overwhelming APIs
        
        # Create cluster-info directory if it doesn't exist
        os.makedirs(self.cluster_info_dir, exist_ok=True)
        
        # Shared KWOK cluster state (ONE container for all logical clusters)
        self.shared_kwok_name = config.get('shared_kwok_cluster_name', 'shared-kwok')
        self.shared_kwok_port = config.get('shared_kwok_api_port', 8050)
        self.shared_kwok_container_id = None
        self.shared_kwok_kubeconfig = None
        self.shared_kwok_network_ip = None
        self.shared_kwok_initialized = False
        
        # Validate authentication (only for ZKS mode)
        if self.import_mode == 'zks':
            has_token = 'zks_token' in config and config['zks_token']
            has_user_pass = ('zks_username' in config and 'zks_password' in config and
                            config['zks_username'] and config['zks_password'])
            
            if not (has_token or has_user_pass):
                raise ValueError("ZKS mode requires either 'zks_token' OR 'zks_username'+'zks_password'")
            
            self.auth_token = config.get('zks_token')
            self.username = config.get('zks_username')
            self.password = config.get('zks_password')
            
            # Validate ZKS kubeconfig path
            if not self.zks_kubeconfig_path or not os.path.exists(self.zks_kubeconfig_path):
                raise FileNotFoundError(f"ZKS kubeconfig not found at: {self.zks_kubeconfig_path}")
            
            self.zedcloud_client = None
            self.zedcloud_project_name = None
        else:
            # Zedcloud mode: validate zedcloud credentials
            if not config.get('zedcloud_url'):
                raise ValueError("Zedcloud mode requires 'zedcloud_url'")
            if not config.get('zedcloud_token'):
                raise ValueError("Zedcloud mode requires 'zedcloud_token'")
            if not config.get('zedcloud_project_name'):
                raise ValueError("Zedcloud mode requires 'zedcloud_project_name'")
            
            # Initialize Zedcloud client
            self.zedcloud_client = ZedcloudClient(
                base_url=config['zedcloud_url'],
                token=config['zedcloud_token']
            )
            self.zedcloud_project_name = config['zedcloud_project_name']
            logger.info(f"Initialized Zedcloud client for project: {self.zedcloud_project_name}")
            
            self.auth_token = None
            self.username = None
            self.password = None
        
        # Validate host kubeconfig
        # Validate host kubeconfig
        if not self.host_kubeconfig or not os.path.exists(self.host_kubeconfig):
            raise FileNotFoundError(f"Host kubeconfig not found at: {self.host_kubeconfig}")
    
    def run_command(self, cmd: list, check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess:
        """Run shell command with logging"""
        logger.debug(f"Running command: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True)
        if result.returncode != 0 and check:
            logger.error(f"Command failed: {result.stderr}")
        return result
    
    def _get_k8s_network_for_context(self, context: str) -> Optional[str]:
        """
        Detect the Docker network for a given Kubernetes context.
        Works with k3d, kind, minikube (docker driver), and Docker Desktop.
        
        Returns:
            Network name if found, None otherwise
        """
        try:
            # Get first node name from the cluster
            result = self.run_command(['kubectl', '--context', context, 'get', 'nodes', '-o', 'jsonpath={.items[0].metadata.name}'], check=False)
            if result.returncode != 0:
                logger.debug(f"Could not get nodes for context {context}")
                return None
            
            node_name = result.stdout.strip()
            logger.debug(f"Context {context} has node: {node_name}")
            
            # Check if this node is a Docker container (k3d, kind, minikube docker)
            result = self.run_command(['docker', 'inspect', node_name, '--format', '{{range $name, $net := .NetworkSettings.Networks}}{{$name}} {{end}}'], check=False)
            if result.returncode != 0:
                logger.debug(f"Node {node_name} is not a Docker container (probably Docker Desktop or remote cluster)")
                return None
            
            # Get first network name (usually the cluster-specific one)
            networks = result.stdout.strip().split()
            if networks:
                network_name = networks[0]
                logger.debug(f"Context {context} uses Docker network: {network_name}")
                return network_name
            
            return None
        except Exception as e:
            logger.debug(f"Error detecting network for context {context}: {e}")
            return None
    
    def _connect_to_host_k8s_network(self, container_name: str, context: str = None) -> str:
        """
        Connect KWOK container to the K8s cluster's Docker network and return the IP.
        Works with any Docker-based K8s distribution (k3d, kind, minikube, Docker Desktop, AWS EKS).
        
        Args:
            container_name: The KWOK container name
            context: The kubernetes context (uses self.host_context if not provided)
            
        Returns:
            Container IP on that network, or host.docker.internal fallback IP
        """
        ctx = context or self.host_context
        logger.info(f"Detecting network for container '{container_name}' in context '{ctx}'")
        
        try:
            # Detect the target cluster's Docker network
            network_name = self._get_k8s_network_for_context(ctx)
            logger.info(f"Detected network: {network_name}")
            
            if network_name:
                # This is a Docker-based K8s (k3d, kind, etc.)
                # Check if already connected to this network
                # Use 'index' function for network names with special characters
                result = self.run_command(['docker', 'inspect', container_name, '--format', f'{{{{index .NetworkSettings.Networks "{network_name}"}}}}'], check=False)
                already_connected = result.returncode == 0 and result.stdout.strip() not in ['<nil>', '<no value>', '']
                logger.info(f"Already connected to {network_name}: {already_connected}")
                
                if not already_connected:
                    # Connect KWOK container to this network
                    result = self.run_command(['docker', 'network', 'connect', network_name, container_name], check=False)
                    if result.returncode != 0:
                        if 'already exists' in result.stderr:
                            logger.debug(f"Container already connected to {network_name}")
                        else:
                            logger.warning(f"Could not connect to {network_name} network: {result.stderr}")
                            # Still try to get IP in case we're already connected
                    else:
                        logger.info(f"✓ Connected KWOK container to {network_name} network")
                else:
                    logger.debug(f"KWOK container already connected to {network_name}")
                
                # Get container's IP on the K8s network
                # Use 'index' function for network names with special characters (hyphens, dots, etc.)
                result = self.run_command(['docker', 'inspect', container_name, '--format', f'{{{{(index .NetworkSettings.Networks "{network_name}").IPAddress}}}}'], check=False)
                if result.returncode == 0 and result.stdout.strip():
                    kwok_ip = result.stdout.strip()
                    logger.info(f"✓ KWOK container IP on {network_name}: {kwok_ip}")
                    return kwok_ip
            
            # AWS VM / EKS / Remote cluster fallback: use host network or bridge
            # For AWS VMs, we need to use the host's IP or bridge network
            logger.debug(f"Using bridge/host network for context {ctx} (AWS VM, Docker Desktop, or remote K8s)")
            
            # Try to get host IP for AWS VM scenarios
            # Check if we're on AWS (check for AWS metadata service)
            try:
                import socket
                # Get hostname/IP that pods can use to reach the host
                # On AWS, this might be the instance's private IP
                host_ip = socket.gethostbyname(socket.gethostname())
                if host_ip and not host_ip.startswith('127.'):
                    logger.info(f"Detected host IP: {host_ip} (AWS VM or remote)")
                    # For AWS, we'll use host IP in kubeconfig, but for now return bridge IP
            except Exception:
                pass
            
            # Get KWOK's IP on bridge network
            result = self.run_command(['docker', 'inspect', container_name, '--format', '{{.NetworkSettings.Networks.bridge.IPAddress}}'], check=False)
            if result.returncode == 0 and result.stdout.strip():
                bridge_ip = result.stdout.strip()
                logger.info(f"✓ KWOK container IP on bridge network: {bridge_ip}")
                return bridge_ip
            
            # Final fallback
            logger.debug("Could not get KWOK IP, using default 172.17.0.1")
            return "172.17.0.1"
            
        except Exception as e:
            logger.debug(f"Network detection failed (using defaults): {e}")
            return "172.17.0.1"
    
    def get_kwok_ip_for_context(self, container_name: str, context: str) -> str:
        """
        Get the KWOK container IP that is reachable from a specific K8s context.
        Connects to the network if needed.
        
        Args:
            container_name: The KWOK container name
            context: The kubernetes context where agents will run
            
        Returns:
            IP address reachable from pods in that context
        """
        return self._connect_to_host_k8s_network(container_name, context)
    
    def initialize_shared_kwok(self) -> str:
        """
        Initialize the shared KWOK cluster (ONE container for all logical clusters)
        This is identical to create_kwok_cluster() but only runs once
        
        Returns:
            Path to shared KWOK kubeconfig
        """
        if self.shared_kwok_initialized:
            logger.info("Shared KWOK cluster already initialized")
            return self.shared_kwok_kubeconfig
        
        container_name = self.shared_kwok_name
        port = self.shared_kwok_port
        
        # Check if container already exists
        result = self.run_command(['docker', 'ps', '-a', '--filter', f'name={container_name}', '--format', '{{.Names}}'], check=False)
        if result.returncode == 0 and result.stdout.strip() == container_name:
            logger.info(f"✓ Shared KWOK container '{container_name}' already exists")
            
            # Check if it's running
            result = self.run_command(['docker', 'ps', '--filter', f'name={container_name}', '--format', '{{.Names}}'], check=False)
            if result.returncode == 0 and result.stdout.strip() == container_name:
                logger.info(f"✓ Shared KWOK container is running")
            else:
                logger.info(f"Starting existing KWOK container: {container_name}")
                self.run_command(['docker', 'start', container_name])
                time.sleep(3)
            
            # Get network IP
            self.shared_kwok_network_ip = self._connect_to_host_k8s_network(container_name, self.host_context)
            
            # Use existing kubeconfig
            kubeconfig_dir = os.path.expanduser(f"~/.kube/kwok-clusters")
            kubeconfig_path = os.path.join(kubeconfig_dir, f"{container_name}.yaml")
            
            if os.path.exists(kubeconfig_path):
                self.shared_kwok_kubeconfig = kubeconfig_path
                self.shared_kwok_initialized = True
                logger.info(f"✓ Using existing kubeconfig: {kubeconfig_path}")
                return kubeconfig_path
            else:
                logger.warning(f"Kubeconfig not found at {kubeconfig_path}, will regenerate")
        
        logger.info(f"Creating shared KWOK container: {self.shared_kwok_name}")
        
        # Start KWOK all-in-one container (EXACT same as original)
        try:
            docker_cmd = [
                'docker', 'run', '-d',
                '--name', container_name,
                '--restart=always',
                '--add-host=host.docker.internal:host-gateway',
            ]
            
            if self.memory_limit:
                docker_cmd.extend(['-m', self.memory_limit])
            
            if self.gogc:
                docker_cmd.extend(['-e', f'GOGC={self.gogc}'])
            
            if self.gomemlimit:
                docker_cmd.extend(['-e', f'GOMEMLIMIT={self.gomemlimit}'])
            
            # Optimized KWOK settings for lightweight operation
            etcd_quota = self.config.get('kwok_etcd_quota', '256Mi')  # Reduced from 512Mi
            docker_cmd.extend([
                '-p', f"{port}:32764",
                self.kwok_image,
                '--kube-apiserver-insecure-port', '0',
                '--etcd-quota-backend-size', etcd_quota,
                '--disable-qps-limits',
                '--kube-admission=false',
                '--heartbeat-factor=10',
                '--extra-args', 'kube-controller-manager=controllers=serviceaccount-controller,serviceaccount-token-controller,namespace-controller,endpoints-controller,replicaset-controller,deployment-controller,daemonset-controller,job-controller,pod-garbage-collector-controller,garbage-collector-controller',
                '--extra-args', 'kube-apiserver=watch-cache-sizes=nodes#0,replicasets.apps#0,daemonsets.apps#0,statefulsets.apps#0,jobs.batch#0,ingresses.networking.k8s.io#0,persistentvolumeclaims#0,persistentvolumes#0',
                '--extra-args', 'kube-apiserver=feature-gates=OpenAPIEnums=false,APIServerIdentity=false,StorageVersionHash=false'
            ])
            
            result = self.run_command(docker_cmd)
            self.shared_kwok_container_id = result.stdout.strip()
            logger.info(f"✓ Shared KWOK container created on port {port}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create shared KWOK container: {e}")
            raise
        
        # Connect to host K8s network
        self.shared_kwok_network_ip = self._connect_to_host_k8s_network(container_name, self.host_context)
        
        # Wait for cluster to be ready
        time.sleep(3)
        
        # Create kubeconfig (EXACT same as original)
        kubeconfig_dir = os.path.expanduser(f"~/.kube/kwok-clusters")
        cert_dir = os.path.join(kubeconfig_dir, container_name)
        os.makedirs(cert_dir, exist_ok=True)
        
        ca_cert_path = os.path.join(cert_dir, 'ca.crt')
        ca_key_path = os.path.join(cert_dir, 'ca.key')
        client_cert_path = os.path.join(cert_dir, 'admin.crt')
        client_key_path = os.path.join(cert_dir, 'admin.key')
        
        # Extract certificates from container (same as original)
        result = subprocess.run(
            ['docker', 'exec', container_name, 'cat', '/root/.kwok/clusters/kwok/pki/ca.crt'],
            capture_output=True, check=True
        )
        with open(ca_cert_path, 'wb') as f:
            f.write(result.stdout)
        
        result = subprocess.run(
            ['docker', 'exec', container_name, 'cat', '/root/.kwok/clusters/kwok/pki/ca.key'],
            capture_output=True, check=True
        )
        with open(ca_key_path, 'wb') as f:
            f.write(result.stdout)
        
        result = subprocess.run(
            ['docker', 'exec', container_name, 'cat', '/root/.kwok/clusters/kwok/pki/admin.crt'],
            capture_output=True, check=True
        )
        with open(client_cert_path, 'wb') as f:
            f.write(result.stdout)
        
        result = subprocess.run(
            ['docker', 'exec', container_name, 'cat', '/root/.kwok/clusters/kwok/pki/admin.key'],
            capture_output=True, check=True
        )
        with open(client_key_path, 'wb') as f:
            f.write(result.stdout)
        
        # Create base kubeconfig
        kubeconfig_path = os.path.join(kubeconfig_dir, f"{container_name}.yaml")
        kubeconfig_content = f"""apiVersion: v1
clusters:
- cluster:
    certificate-authority: {ca_cert_path}
    server: https://127.0.0.1:{port}
  name: {container_name}
contexts:
- context:
    cluster: {container_name}
    user: {container_name}
  name: {container_name}
current-context: {container_name}
kind: Config
preferences: {{}}
users:
- name: {container_name}
  user:
    client-certificate: {client_cert_path}
    client-key: {client_key_path}
"""
        with open(kubeconfig_path, 'w') as f:
            f.write(kubeconfig_content)
        
        self.shared_kwok_kubeconfig = kubeconfig_path
        self.shared_kwok_initialized = True
        
        logger.info(f"✓ Shared KWOK initialized: {kubeconfig_path}")
        return kubeconfig_path
    
    def is_port_in_use(self, port: int) -> bool:
        """Check if a port is already in use"""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('', port))
                return False
            except OSError:
                return True
    
    def allocate_port(self) -> int:
        """Allocate next available port for KWOK cluster"""
        max_attempts = 1000
        for _ in range(max_attempts):
            port = self.port_counter
            self.port_counter += 1
            if not self.is_port_in_use(port):
                logger.debug(f"Allocated available port: {port}")
                return port
        raise RuntimeError(f"Could not find available port after {max_attempts} attempts starting from {self.kwok_base_port}")
        return port
    
    def create_kwok_cluster(self, cluster_name: str, num_nodes: int = 10) -> Dict[str, str]:
        """
        Create a logical cluster in shared KWOK (namespace + nodes + namespace-scoped kubeconfig)
        
        Args:
            cluster_name: Name for the logical cluster  
            num_nodes: Number of fake nodes to create
            
        Returns:
            Dict containing cluster info (name, port, kubeconfig_path, network_ip)
        """
        logger.info(f"Creating logical cluster in shared KWOK: {cluster_name} with {num_nodes} nodes")
        
        # Ensure shared KWOK is initialized
        if not self.shared_kwok_initialized:
            raise RuntimeError("Shared KWOK not initialized! Call initialize_shared_kwok() first")
        
        # 1. Create namespace in shared KWOK
        namespace_yaml = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {cluster_name}
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(namespace_yaml)
            ns_file = f.name
        
        try:
            self.run_command(['kubectl', '--kubeconfig', self.shared_kwok_kubeconfig, 'apply', '-f', ns_file], capture_output=True)
        finally:
            os.unlink(ns_file)
        
        # 2. Create nodes with unique names and cluster labels (nodes are cluster-scoped!)
        template_path = os.path.join(os.path.dirname(__file__), 'config', 'fake-node-template.yaml')
        with open(template_path, 'r') as f:
            node_template = f.read()
        
        for i in range(num_nodes):
            # Unique node name per logical cluster: cluster-name-node-001
            node_name = f"{cluster_name}-node-{i+1:03d}"
            
            # Check if node already exists to prevent duplicates on restart
            check_result = self.run_command(
                ['kubectl', '--kubeconfig', self.shared_kwok_kubeconfig, 
                 'get', 'node', node_name, '-o', 'name'],
                check=False, capture_output=True
            )
            
            if check_result.returncode == 0:
                logger.debug(f"Node {node_name} already exists, skipping creation")
                continue
            
            node_manifest = node_template.replace('REPLACE_NODE_NAME', node_name)
            
            # Add cluster identification labels by appending to existing labels section
            # Find the labels: line and add our cluster labels after it
            import yaml
            node_dict = yaml.safe_load(node_manifest)
            if 'metadata' not in node_dict:
                node_dict['metadata'] = {}
            if 'labels' not in node_dict['metadata']:
                node_dict['metadata']['labels'] = {}
            
            # Add cluster identification labels
            node_dict['metadata']['labels']['cluster-id'] = cluster_name
            node_dict['metadata']['labels']['logical-cluster'] = cluster_name
            node_dict['metadata']['labels']['kwok.x-k8s.io/node'] = 'fake'
            
            node_manifest = yaml.dump(node_dict, default_flow_style=False)
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(node_manifest)
                node_file = f.name
            
            try:
                self.run_command(['kubectl', '--kubeconfig', self.shared_kwok_kubeconfig, 
                                 'apply', '-f', node_file], capture_output=True)
                logger.debug(f"Created node: {node_name}")
            finally:
                os.unlink(node_file)
        
        logger.info(f"✓ Created {num_nodes} nodes with unique names for logical cluster {cluster_name}")
        
        # 3. Create namespace-scoped kubeconfig
        # Use template (same as original) with certs from shared KWOK
        kubeconfig_path = os.path.join(self.cluster_info_dir, f"{cluster_name}-kubeconfig.yaml")
        
        # Copy cert files to cluster-specific directory (for consistency with original)
        cluster_cert_dir = os.path.join(self.cluster_info_dir, f"{cluster_name}-certs")
        os.makedirs(cluster_cert_dir, exist_ok=True)
        
        # Copy certs from shared KWOK cert directory
        # self.shared_kwok_kubeconfig = ~/.kube/kwok-clusters/shared-kwok.yaml
        # So dirname = ~/.kube/kwok-clusters, and we want ~/.kube/kwok-clusters/shared-kwok
        shared_cert_dir = os.path.join(os.path.dirname(self.shared_kwok_kubeconfig), self.shared_kwok_name)
        
        import shutil
        # Use absolute paths for cert files
        ca_cert_path = os.path.abspath(os.path.join(cluster_cert_dir, 'ca.crt'))
        ca_key_path = os.path.abspath(os.path.join(cluster_cert_dir, 'ca.key'))
        client_cert_path = os.path.abspath(os.path.join(cluster_cert_dir, 'admin.crt'))
        client_key_path = os.path.abspath(os.path.join(cluster_cert_dir, 'admin.key'))
        
        shutil.copy(os.path.join(shared_cert_dir, 'ca.crt'), ca_cert_path)
        shutil.copy(os.path.join(shared_cert_dir, 'ca.key'), ca_key_path)
        shutil.copy(os.path.join(shared_cert_dir, 'admin.crt'), client_cert_path)
        shutil.copy(os.path.join(shared_cert_dir, 'admin.key'), client_key_path)
        
        # Create kubeconfig using template pattern (same as original)
        template_path = os.path.join(os.path.dirname(__file__), 'config', 'kubeconfig-template.yaml')
        with open(template_path, 'r') as f:
            kubeconfig_template = f.read()
        
        # Create kubeconfig with 127.0.0.1 (for Rancher import from our machine)
        kubeconfig_content = kubeconfig_template.replace('REPLACE_CLUSTER_NAME', cluster_name)
        kubeconfig_content = kubeconfig_content.replace('REPLACE_PORT', str(self.shared_kwok_port))
        kubeconfig_content = kubeconfig_content.replace('REPLACE_CA_CERT_PATH', ca_cert_path)
        kubeconfig_content = kubeconfig_content.replace('REPLACE_CLIENT_CERT_PATH', client_cert_path)
        kubeconfig_content = kubeconfig_content.replace('REPLACE_CLIENT_KEY_PATH', client_key_path)
        
        with open(kubeconfig_path, 'w') as f:
            f.write(kubeconfig_content)
        
        return {
            'name': cluster_name,
            'port': self.shared_kwok_port,  # All logical clusters use same port
            'container_id': self.shared_kwok_container_id,
            'container_name': self.shared_kwok_name,
            'kubeconfig_path': kubeconfig_path,
            'network_ip': self.shared_kwok_network_ip
        }
    
    def import_cluster_to_zks(self, cluster_name: str) -> Dict[str, str]:
        """
        Import cluster to ZKS via API
        
        Args:
            cluster_name: Name of the cluster to import
            
        Returns:
            Dict containing cluster_id and registration_token
        """
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        logger.info(f"Importing cluster '{cluster_name}' to ZKS...")
        
        # Prepare headers
        headers = {'Content-Type': 'application/json'}
        if self.auth_token:
            headers['Authorization'] = f'Bearer {self.auth_token}'
        
        # Create cluster import entry
        payload = {
            "type": "cluster",
            "name": cluster_name,
            "description": f"KWOK simulated cluster {cluster_name}"
        }
        
        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)
        
        try:
            # Create cluster
            resp = requests.post(
                f"{self.zks_url}/v3/clusters",
                json=payload,
                headers=headers,
                auth=auth,
                verify=False,
                timeout=30
            )
            resp.raise_for_status()
            cluster_data = resp.json()
            cluster_id = cluster_data['id']
            logger.info(f"✓ Cluster created with ID: {cluster_id}")
            
            # Wait for registration token to be created
            # Use cluster-specific endpoint for faster, more reliable token retrieval
            logger.info("Waiting for registration token generation...")
            time.sleep(5)  # Initial 5s delay to let ZKS start token creation
            
            registration_token = None
            max_token_attempts = 30  # 30 attempts × 2 seconds = 60 seconds total
            for attempt in range(max_token_attempts):
                time.sleep(2)
                # Query cluster-specific tokens (much faster than scanning all tokens)
                resp = requests.get(
                    f"{self.zks_url}/v3/clusters/{cluster_id}/clusterregistrationtokens",
                    headers=headers,
                    auth=auth,
                    verify=False,
                    timeout=30
                )
                resp.raise_for_status()
                tokens = resp.json().get('data', [])
                
                # Look for default-token (auto-created for imported clusters)
                for token_data in tokens:
                    if token_data.get('name') == 'default-token':
                        registration_token = token_data.get('token')
                        logger.info(f"✓ Registration token obtained after {attempt + 1} attempts ({(attempt + 1) * 2}s)")
                        break
                
                if registration_token:
                    break
                
                if attempt < max_token_attempts - 1:
                    if (attempt + 1) % 10 == 0:  # Log every 10 attempts
                        logger.info(f"Still waiting for token... ({attempt + 1}/{max_token_attempts} attempts, {(attempt + 1) * 3}s elapsed)")
                    else:
                        logger.debug(f"Token not ready, retrying... ({attempt + 1}/{max_token_attempts})")
            
            if not registration_token:
                raise RuntimeError(f"Failed to get registration token after {max_token_attempts} attempts")
            
            logger.info(f"✓ Registration token obtained")
            
            return {
                'cluster_id': cluster_id,
                'registration_token': registration_token
            }
            
        except requests.RequestException as e:
            logger.error(f"Failed to import cluster: {e}")
            raise
    
    def import_cluster_to_zedcloud(self, cluster_name: str) -> Dict[str, str]:
        """
        Import cluster to Zedcloud using REST API
        
        Args:
            cluster_name: Name of the cluster to import
            
        Returns:
            dict: Contains cluster_id, object_id, manifest_url, registration_token
        """
        logger.info(f"Importing cluster to Zedcloud: {cluster_name}")
        
        try:
            # Import the cluster via Zedcloud API
            object_id = self.zedcloud_client.import_cluster(
                name=cluster_name,
                title=cluster_name,
                project_name=self.zedcloud_project_name
            )
            
            logger.info(f"✓ Cluster imported to Zedcloud (object_id: {object_id})")
            
            # Get registration commands and manifest URL
            reg_commands = self.zedcloud_client.get_registration_commands(object_id)
            
            manifest_url = reg_commands.get('manifest_url')
            if not manifest_url:
                raise ValueError("No manifest URL returned from Zedcloud")
            
            # Extract cluster_id from manifest URL
            # URL format: https://zks2.thor.zededa.dev/v3/import/{token}_{cluster_id}.yaml
            import re
            match = re.search(r'_([a-z0-9-]+)\.yaml$', manifest_url)
            if match:
                cluster_id = match.group(1)
                logger.info(f"✓ Registration manifest obtained (cluster_id: {cluster_id})")
            else:
                cluster_id = ''
                logger.warning(f"⚠️  Could not extract cluster_id from manifest URL")
                logger.info(f"✓ Registration manifest obtained")
            
            return {
                'cluster_id': cluster_id,
                'object_id': object_id,
                'manifest_url': manifest_url
            }
            
        except Exception as e:
            logger.error(f"Failed to import cluster to Zedcloud: {e}")
            raise
    
    def wait_for_cluster_active(self, cluster_id: str = None, cluster_name: str = None, timeout: int = 300) -> bool:
        """
        Wait for cluster to become Active (mode-aware)
        
        Args:
            cluster_id: Cluster ID (used for ZKS mode)
            cluster_name: Cluster name (used for Zedcloud mode)
            timeout: Maximum time to wait in seconds
            
        Returns:
            bool: True if cluster is Active, False if timeout
        """
        logger.info(f"Waiting for cluster to become Active (timeout: {timeout}s)...")
        
        start_time = time.time()
        check_interval = 5
        
        if self.import_mode == 'zedcloud':
            # Zedcloud mode: use Zedcloud API
            if not cluster_name:
                logger.error("cluster_name required for Zedcloud mode status check")
                return False
            
            while time.time() - start_time < timeout:
                try:
                    status = self.zedcloud_client.get_cluster_status(cluster_name)
                    run_state = status.get('runState', '')
                    
                    # Check if cluster is online
                    if run_state == 'RUN_STATE_ONLINE':
                        logger.info(f"✓ Cluster is Active! (runState: {run_state})")
                        return True
                    
                    logger.debug(f"Cluster runState: {run_state}, waiting...")
                    time.sleep(check_interval)
                    
                except Exception as e:
                    logger.debug(f"Error checking cluster status: {e}")
                    time.sleep(check_interval)
            
            logger.warning(f"✗ Cluster did not become Active within {timeout}s")
            return False
        else:
            # ZKS mode: use ZKS API directly
            import requests
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            
            if not cluster_id:
                logger.error("cluster_id required for ZKS mode status check")
                return False
            
            headers = {'Content-Type': 'application/json'}
            if self.auth_token:
                headers['Authorization'] = f'Bearer {self.auth_token}'
            
            auth = None
            if self.username and self.password:
                auth = (self.username, self.password)
            
            state = ''
            while time.time() - start_time < timeout:
                try:
                    resp = requests.get(
                        f"{self.zks_url}/v3/clusters/{cluster_id}",
                        headers=headers,
                        auth=auth,
                        verify=False,
                        timeout=10
                    )
                    resp.raise_for_status()
                    cluster_data = resp.json()
                    
                    # Check if cluster state is 'active'
                    state = cluster_data.get('state', '')
                    
                    if state == 'active':
                        logger.info(f"✓ Cluster is Active!")
                        return True
                    
                    logger.debug(f"Cluster state: {state}, waiting...")
                    time.sleep(check_interval)
                    
                except Exception as e:
                    logger.debug(f"Error checking cluster status: {e}")
                    time.sleep(check_interval)
            
            logger.warning(f"✗ Cluster did not become Active within {timeout}s (last state: {state})")
            return False
    
    def _get_kwok_api_server_url(self, cluster_name: str) -> str:
        """
        Get the KWOK cluster's API server URL and convert it for pod access.
        
        Args:
            cluster_name: Name of the KWOK cluster
            
        Returns:
            API server URL accessible from pod (using host.docker.internal on macOS/Linux)
        """
        import re
        
        # Get kubeconfig path
        kubeconfig_path = os.path.expanduser(f"~/.kube/kwok-clusters/{cluster_name}.yaml")
        
        result = subprocess.run(
            ['kubectl', '--kubeconfig', kubeconfig_path, 'cluster-info'],
            capture_output=True, text=True, check=True
        )
        
        # Extract URL from output like "Kubernetes control plane is running at https://127.0.0.1:32766"
        for line in result.stdout.split('\n'):
            if 'running at' in line:
                server_url = line.split('running at')[1].strip()
                # Remove ANSI color codes
                server_url = re.sub(r'\x1b\[[0-9;]*m', '', server_url)
                # Replace localhost with host.docker.internal for pod access
                return server_url.replace('127.0.0.1', 'host.docker.internal').replace('localhost', 'host.docker.internal')
        
        raise ValueError(f"Could not find API server URL for cluster {cluster_name}")
    
    def _extract_ca_files(self, kubeconfig: dict, target_cluster_name: str) -> tuple:
        """
        Extract CA certificate and key from KWOK kubeconfig.
        
        Args:
            kubeconfig: Parsed kubeconfig dict
            target_cluster_name: Name of the cluster to extract CA from
            
        Returns:
            Tuple of (ca_cert_base64, ca_key_base64) or (None, None)
        """
        import base64
        
        for cluster in kubeconfig.get('clusters', []):
            if cluster.get('name') != target_cluster_name:
                continue
                
            ca_path = cluster['cluster'].get('certificate-authority')
            if not ca_path or not os.path.exists(ca_path):
                return None, None
            
            # Read CA cert
            with open(ca_path, 'rb') as f:
                ca_cert = base64.b64encode(f.read()).decode()
            
            # Read CA key (same directory, ca.key instead of ca.crt)
            ca_key_path = ca_path.replace('ca.crt', 'ca.key')
            ca_key = None
            if os.path.exists(ca_key_path):
                with open(ca_key_path, 'rb') as f:
                    ca_key = base64.b64encode(f.read()).decode()
            
            return ca_cert, ca_key
        
        return None, None
    
    def _prepare_kubeconfig_for_pod(self, kubeconfig: dict, target_cluster_name: str, server_url: str) -> None:
        """
        Modify kubeconfig for pod use: set server URL, enable insecure TLS, remove CA refs.
        This modifies the kubeconfig dict in place.
        
        Args:
            kubeconfig: Parsed kubeconfig dict
            target_cluster_name: Name of the cluster to modify
            server_url: API server URL accessible from pod
        """
        for cluster in kubeconfig.get('clusters', []):
            if cluster.get('name') != target_cluster_name:
                continue
            
            # Set server URL accessible from pod
            cluster['cluster']['server'] = server_url
            
            # Enable insecure TLS (KWOK cert doesn't include host.docker.internal)
            cluster['cluster']['insecure-skip-tls-verify'] = True
            
            # Remove CA references (client-go rejects them with insecure-skip-tls-verify)
            cluster['cluster'].pop('certificate-authority', None)
            cluster['cluster'].pop('certificate-authority-data', None)
    
    def _embed_client_certs(self, kubeconfig: dict) -> None:
        """
        Embed client certificates as base64 data instead of file paths.
        This modifies the kubeconfig dict in place.
        
        Args:
            kubeconfig: Parsed kubeconfig dict
        """
        import base64
        
        for user in kubeconfig.get('users', []):
            user_data = user.get('user', {})
            
            # Embed client certificate
            cert_path = user_data.get('client-certificate')
            if cert_path and os.path.exists(cert_path):
                with open(cert_path, 'rb') as f:
                    user_data['client-certificate-data'] = base64.b64encode(f.read()).decode()
                del user_data['client-certificate']
            
            # Embed client key
            key_path = user_data.get('client-key')
            if key_path and os.path.exists(key_path):
                with open(key_path, 'rb') as f:
                    user_data['client-key-data'] = base64.b64encode(f.read()).decode()
                del user_data['client-key']
    
    def _fix_kubeconfig_for_pod(self, kubeconfig_content: str, cluster_name: str) -> tuple:
        """
        Transform KWOK kubeconfig for pod deployment.
        
        Pod deployment requirements:
        - Server URL must use host.docker.internal (works on macOS/Linux with --add-host flag)
        - Client certs must be embedded (file paths don't exist in pod)
        - insecure-skip-tls-verify must be true (cert doesn't include host.docker.internal)
        - CA cert/key must be extracted separately (client-go rejects them with insecure TLS)
        
        Args:
            kubeconfig_content: Original kubeconfig YAML content
            cluster_name: Name of the KWOK cluster
            
        Returns:
            Tuple of (kubeconfig_yaml, ca_cert_base64, ca_key_base64)
        """
        import yaml
        
        kubeconfig = yaml.safe_load(kubeconfig_content)
        
        # Get current context to identify target cluster
        current_context = kubeconfig.get('current-context')
        if not current_context:
            raise ValueError("No current-context found in kubeconfig")
        
        # Find cluster name from context
        target_cluster_name = None
        for ctx in kubeconfig.get('contexts', []):
            if ctx.get('name') == current_context:
                target_cluster_name = ctx['context'].get('cluster')
                break
        
        if not target_cluster_name:
            raise ValueError(f"Could not find cluster for context {current_context}")
        
        # Determine which port to use based on the host K8s cluster type
        # - Docker Desktop: pods access via host, use mapped port (8050)
        # - k3d/kind/minikube: pods access via Docker network, use container internal port (32764)
        # - AWS VM/EKS: pods access via host IP, use mapped port (8050) or host network
        network_name = self._get_k8s_network_for_context(self.host_context)
        if network_name:
            # Docker-based K8s (k3d, kind, etc.) - pods access container directly
            kwok_port = 32764  # KWOK's internal API server port
            logger.info(f"Using container-internal port {kwok_port} for Docker-based K8s ({network_name})")
            server_url = f"https://{self.shared_kwok_network_ip}:{kwok_port}"
        else:
            # Docker Desktop, AWS VM, or remote K8s - pods access via host
            kwok_port = self.shared_kwok_port  # Host-mapped port (8050)
            logger.info(f"Using host-mapped port {kwok_port} for Docker Desktop/AWS VM/Remote K8s")
            # For AWS VMs, try to use host IP instead of host.docker.internal
            # Check if host.docker.internal is available, otherwise use bridge IP
            try:
                import socket
                socket.gethostbyname('host.docker.internal')
                server_url = f"https://host.docker.internal:{kwok_port}"
            except socket.gaierror:
                # host.docker.internal not available (AWS VM scenario)
                # Use the bridge network IP we detected earlier
                if self.shared_kwok_network_ip:
                    server_url = f"https://{self.shared_kwok_network_ip}:{kwok_port}"
                else:
                    server_url = f"https://host.docker.internal:{kwok_port}"  # Fallback
        
        # Extract CA files before modifying kubeconfig
        ca_cert, ca_key = self._extract_ca_files(kubeconfig, target_cluster_name)
        
        # Modify kubeconfig for pod use
        self._prepare_kubeconfig_for_pod(kubeconfig, target_cluster_name, server_url)
        self._embed_client_certs(kubeconfig)
        
        return yaml.dump(kubeconfig), ca_cert, ca_key
    
    def start_agent_simulator_pod(self, cluster_info: Dict[str, str], import_info: Dict[str, str], 
                                  host_kubeconfig: Optional[str] = None) -> str:
        """
        Deploy agent simulator as a pod in a Kubernetes cluster
        
        Args:
            cluster_info: Dict with 'name', 'port', 'kubeconfig_path'
            import_info: Dict with 'cluster_id', 'registration_token'
            host_kubeconfig: Kubeconfig for the host K8s cluster (optional, uses current context)
            
        Returns:
            str: Deployment name
        """
        import base64
        
        cluster_name = cluster_info['name']
        cluster_id = import_info['cluster_id']
        
        logger.info(f"Deploying agent simulator pod for cluster '{cluster_name}'...")
        
        # Read template
        template_path = os.path.join(os.path.dirname(__file__), 'config', 'agent-deployment-template.yaml')
        with open(template_path, 'r') as f:
            deployment_template = f.read()
        
        # Read and fix KWOK kubeconfig for pod use
        with open(cluster_info['kubeconfig_path'], 'r') as f:
            kwok_kubeconfig_raw = f.read()
        
        # Fix kubeconfig: embed certificates and fix server URL for pod access
        # Returns tuple: (kubeconfig_yaml, ca_cert_data_base64, ca_key_data_base64)
        kwok_kubeconfig, ca_cert_data, ca_key_data = self._fix_kubeconfig_for_pod(kwok_kubeconfig_raw, cluster_name)
        
        # Indent the kubeconfig content for YAML (4 spaces for ConfigMap data)
        kwok_kubeconfig_indented = '\n'.join('    ' + line if line.strip() else '' 
                                              for line in kwok_kubeconfig.split('\n'))
        
        # Decode and indent CA cert data if present (for ConfigMap)
        ca_cert_indented = ''
        if ca_cert_data:
            # Decode base64 to PEM format
            ca_cert_pem = base64.b64decode(ca_cert_data).decode('utf-8')
            ca_cert_indented = '\n'.join('    ' + line if line.strip() else ''
                                          for line in ca_cert_pem.split('\n'))
        
        # Decode and indent CA key data if present (for ConfigMap)
        ca_key_indented = ''
        if ca_key_data:
            # Decode base64 to PEM format
            ca_key_pem = base64.b64decode(ca_key_data).decode('utf-8')
            ca_key_indented = '\n'.join('    ' + line if line.strip() else ''
                                         for line in ca_key_pem.split('\n'))
        
        # Read ZKS kubeconfig
        with open(self.zks_kubeconfig_path, 'r') as f:
            zks_kubeconfig = f.read()
        
        # Base64 encode ZKS kubeconfig for Secret
        zks_kubeconfig_b64 = base64.b64encode(zks_kubeconfig.encode()).decode()
        
        # Extract hostname from zks_url
        zks_host = self.zks_url.replace('https://', '').replace('http://', '').split('/')[0]
        
        # Get KWOK container IP on K8s network for hostAliases
        # This dynamically gets the correct IP for the current host context
        # (handles k3d, kind, Docker Desktop, etc.)
        kwok_network_ip = self.get_kwok_ip_for_context(self.shared_kwok_name, self.host_context)
        logger.info(f"Using KWOK network IP {kwok_network_ip} for context {self.host_context}")
        
        # Allocate unique steve port for this cluster
        steve_port = self.allocate_port()
        logger.info(f"Allocated steve port {steve_port} for cluster {cluster_name}")
        
        # Replace placeholders
        deployment_yaml = deployment_template.replace('CLUSTER_NAME', cluster_name)
        deployment_yaml = deployment_yaml.replace('KWOK_KUBECONFIG_CONTENT', kwok_kubeconfig_indented)
        deployment_yaml = deployment_yaml.replace('CA_CERT_CONTENT', ca_cert_indented if ca_cert_data else '')
        deployment_yaml = deployment_yaml.replace('CA_KEY_CONTENT', ca_key_indented if ca_key_data else '')
        deployment_yaml = deployment_yaml.replace('ZKS_KUBECONFIG_BASE64', zks_kubeconfig_b64)
        deployment_yaml = deployment_yaml.replace('KWOK_CONTEXT', cluster_name)
        deployment_yaml = deployment_yaml.replace('CLUSTER_ID', cluster_id)
        deployment_yaml = deployment_yaml.replace('ZKS_HOST', zks_host)
        deployment_yaml = deployment_yaml.replace('AGENT_IMAGE', self.agent_image)
        deployment_yaml = deployment_yaml.replace('KWOK_NETWORK_IP', kwok_network_ip)
        deployment_yaml = deployment_yaml.replace('POLL_INTERVAL', str(self.agent_poll_interval))
        deployment_yaml = deployment_yaml.replace('STEVE_PORT', str(steve_port))
        
        # Save to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(deployment_yaml)
            temp_file = f.name
        
        try:
            # Apply to host cluster (not KWOK cluster!)
            # Must use explicit kubeconfig AND context to avoid deploying to wrong cluster
            if not host_kubeconfig:
                raise ValueError("host_kubeconfig must be specified when using pod agents to avoid deploying to wrong cluster")
            
            # Use explicit context to ensure we deploy to the right cluster
            kubectl_cmd = ['kubectl', 'apply', '-f', temp_file,
                          '--kubeconfig', host_kubeconfig,
                          '--context', self.host_context]
            
            result = subprocess.run(kubectl_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"kubectl apply failed with stderr: {result.stderr}")
                logger.error(f"kubectl apply stdout: {result.stdout}")
                raise subprocess.CalledProcessError(result.returncode, kubectl_cmd, result.stdout, result.stderr)
            
            # Wait for pod to be ready
            deployment_name = f"zks-agent-{cluster_name}"
            logger.info(f"Waiting for deployment {deployment_name} to be ready...")
            
            wait_cmd = ['kubectl', 'wait', '--for=condition=available', '--timeout=60s',
                       f'deployment/{deployment_name}', '-n', 'zks-simulator',
                       '--kubeconfig', host_kubeconfig,
                       '--context', self.host_context]
            
            self.run_command(wait_cmd, check=True)
            
            logger.info(f"✓ Agent simulator pod deployed: {deployment_name}")
            logger.info(f"  View logs: kubectl logs -n zks-simulator deployment/{deployment_name}")
            
            return deployment_name
            
        finally:
            os.unlink(temp_file)
    
    def cleanup_agent_pod(self, cluster_name: str, host_kubeconfig: Optional[str] = None) -> None:
        """
        Delete agent simulator pod deployment
        
        Args:
            cluster_name: Name of the cluster
            host_kubeconfig: Kubeconfig for the host K8s cluster (optional)
        """
        deployment_name = f"zks-agent-{cluster_name}"
        configmap_name = f"kwok-kubeconfig-{cluster_name}"
        
        logger.info(f"Deleting agent pod deployment: {deployment_name}")
        
        if not host_kubeconfig:
            raise ValueError("host_kubeconfig must be specified when using pod agents")
        
        kubectl_base = ['kubectl', '--kubeconfig', host_kubeconfig, '--context', self.host_context]
        
        # Delete deployment
        self.run_command(kubectl_base + ['delete', 'deployment', deployment_name, 
                                        '-n', 'zks-simulator'], check=False)
        
        # Delete configmap
        self.run_command(kubectl_base + ['delete', 'configmap', configmap_name,
                                        '-n', 'zks-simulator'], check=False)
        
        logger.info(f"✓ Agent pod resources deleted")
    
    def apply_registration_manifest(self, cluster_info: Dict[str, str], manifest_url: str, cluster_id: str = None) -> None:
        """
        Apply registration manifest to KWOK cluster
        
        Args:
            cluster_info: Cluster info with context
            manifest_url: URL to the registration manifest
            cluster_id: Cluster ID to label the cattle-credentials secret
        """
        logger.info(f"Applying registration manifest to cluster...")
        
        # Download and apply manifest
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        try:
            # Download manifest
            response = requests.get(manifest_url, verify=False, timeout=30)
            response.raise_for_status()
            manifest_content = response.text
            
            # Transform manifest to add cluster-id label to cattle-credentials secret
            if cluster_id:
                manifest_content = self.add_cluster_label_to_manifest(manifest_content, cluster_id)
            
            # Save to temp file and apply
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(manifest_content)
                temp_file = f.name
            
            try:
                self.run_command([
                    'kubectl', '--kubeconfig', cluster_info['kubeconfig_path'],
                    'apply', '-f', temp_file
                ], capture_output=True)
                logger.info(f"✓ Registration manifest applied")
            finally:
                os.unlink(temp_file)
                
        except Exception as e:
            logger.error(f"Failed to apply registration manifest: {e}")
            raise
    
    def add_cluster_label_to_manifest(self, manifest_content: str, cluster_id: str) -> str:
        """
        Add cluster-id label to cattle-credentials secret in the manifest
        
        Args:
            manifest_content: Original YAML manifest
            cluster_id: Cluster ID to add as label
            
        Returns:
            Modified YAML manifest
        """
        import yaml
        
        try:
            # Parse YAML documents (manifest can contain multiple)
            docs = list(yaml.safe_load_all(manifest_content))
            
            # Find and modify cattle-credentials secret
            for doc in docs:
                if doc and doc.get('kind') == 'Secret' and doc.get('metadata', {}).get('name', '').startswith('cattle-credentials-'):
                    # Add label with cluster-id
                    if 'metadata' not in doc:
                        doc['metadata'] = {}
                    if 'labels' not in doc['metadata']:
                        doc['metadata']['labels'] = {}
                    doc['metadata']['labels']['cluster-id'] = cluster_id
                    logger.info(f"Added cluster-id label to cattle-credentials secret: {cluster_id}")
            
            # Convert back to YAML
            output = []
            for doc in docs:
                if doc:
                    output.append(yaml.dump(doc, default_flow_style=False))
            
            return '---\n'.join(output)
            
        except Exception as e:
            logger.warning(f"Failed to add cluster-id label to manifest: {e}")
            # Return original manifest if transformation fails
            return manifest_content
    
    def create_and_register_cluster(self, cluster_name: str, num_nodes: int = 10, 
                                   start_agent: bool = True) -> Dict[str, Any]:
        """
        Complete workflow: Create KWOK cluster, import to ZKS, start agent
        
        Args:
            cluster_name: Name for the cluster
            num_nodes: Number of fake nodes
            start_agent: Whether to start agent simulator
            
        Returns:
            Dict with all cluster information
        """
        logger.info(f"=" * 80)
        logger.info(f"Creating and registering KWOK cluster: {cluster_name}")
        logger.info(f"=" * 80)
        
        # Step 1: Create KWOK cluster
        cluster_info = self.create_kwok_cluster(cluster_name, num_nodes)
        
        # Step 2: Import cluster (route based on mode)
        if self.import_mode == 'zedcloud':
            logger.info(f"Using Zedcloud import mode")
            import_info = self.import_cluster_to_zedcloud(cluster_name)
            manifest_url = import_info['manifest_url']
        else:
            logger.info(f"Using ZKS import mode")
            import_info = self.import_cluster_to_zks(cluster_name)
            manifest_url = f"{self.zks_url}/v3/import/{import_info['registration_token']}_{import_info['cluster_id']}.yaml"
        
        # Save cluster_id immediately so cleanup can find it if we fail
        partial_info_file = os.path.join(self.cluster_info_dir, f"{cluster_name}-info.json")
        partial_info = {
            'name': cluster_name,
            'cluster_id': import_info['cluster_id'],
            'port': cluster_info['port'],
            'kubeconfig_path': cluster_info['kubeconfig_path']
        }
        # Add object_id for Zedcloud mode
        if 'object_id' in import_info:
            partial_info['object_id'] = import_info['object_id']
        
        with open(partial_info_file, 'w') as f:
            json.dump(partial_info, f, indent=2)
        logger.info(f"Saved cluster_id for cleanup: {import_info['cluster_id']}")
        
        # Step 3: Apply registration manifest
        self.apply_registration_manifest(cluster_info, manifest_url, import_info['cluster_id'])
        
        # Step 4: Start agent simulator pod
        deployment_name = None
        if start_agent:
            deployment_name = self.start_agent_simulator_pod(cluster_info, import_info, self.host_kubeconfig)
        
        # Step 5: Wait for cluster to become Active (mode-aware)
        if self.import_mode == 'zedcloud':
            is_active = self.wait_for_cluster_active(cluster_name=cluster_name, timeout=300)
        else:
            is_active = self.wait_for_cluster_active(cluster_id=import_info['cluster_id'], timeout=300)
        
        result = {
            **cluster_info,
            **import_info,
            'agent_deployment': deployment_name,
            'is_active': is_active
        }
        
        logger.info(f"=" * 80)
        if is_active:
            logger.info(f"✅ Cluster '{cluster_name}' is ACTIVE and ready!")
        else:
            logger.warning(f"⚠️  Cluster '{cluster_name}' created but not Active yet")
        logger.info(f"  Cluster ID: {import_info['cluster_id']}")
        logger.info(f"  Port: {cluster_info['port']}")
        logger.info(f"  Kubeconfig: {cluster_info['kubeconfig_path']}")
        logger.info(f"  Nodes: {num_nodes}")
        if deployment_name:
            logger.info(f"  Agent Deployment: {deployment_name}")
        logger.info(f"=" * 80)
        
        return result
    
    def delete_cluster_from_zedcloud(self, object_id: str) -> bool:
        """
        Delete cluster from Zedcloud
        
        Args:
            object_id: Zedcloud cluster object ID (UUID)
            
        Returns:
            bool: True if successful
        """
        if not self.zedcloud_client:
            logger.error("Zedcloud client not initialized")
            return False
        
        logger.info(f"Deleting cluster with object_id '{object_id}' from Zedcloud...")
        
        try:
            success = self.zedcloud_client.delete_cluster(object_id)
            if success:
                logger.info(f"✓ Cluster deleted from Zedcloud")
            return success
        except Exception as e:
            logger.error(f"Failed to delete cluster from Zedcloud: {e}")
            return False
    
    def delete_cluster_from_zks(self, cluster_id: str, wait_for_deletion: bool = True) -> bool:
        """
        Delete cluster from ZKS
        
        Args:
            cluster_id: Cluster ID to delete
            wait_for_deletion: Whether to wait and verify cluster is deleted
            
        Returns:
            bool: True if successful
        """
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        logger.info(f"Deleting cluster '{cluster_id}' from ZKS...")
        
        headers = {'Content-Type': 'application/json'}
        if self.auth_token:
            headers['Authorization'] = f'Bearer {self.auth_token}'
        
        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)
        
        try:
            # Send DELETE request
            resp = requests.delete(
                f"{self.zks_url}/v3/clusters/{cluster_id}",
                headers=headers,
                auth=auth,
                verify=False,
                timeout=30
            )
            
            if resp.status_code not in [200, 204, 202]:
                logger.warning(f"DELETE request returned: {resp.status_code} - {resp.text}")
                return False
            
            logger.info(f"✓ DELETE request sent for cluster {cluster_id}")
            
            # Wait for cluster to be fully deleted
            if wait_for_deletion:
                logger.info(f"Waiting for cluster to be deleted...")
                max_wait = 30  # 30 seconds max
                for _ in range(max_wait):
                    try:
                        check_resp = requests.get(
                            f"{self.zks_url}/v3/clusters/{cluster_id}",
                            headers=headers,
                            auth=auth,
                            verify=False,
                            timeout=10
                        )
                        if check_resp.status_code == 404:
                            logger.info(f"✓ Cluster deleted from ZKS")
                            return True
                        time.sleep(1)
                    except requests.RequestException:
                        # Cluster might be deleted
                        logger.info(f"✓ Cluster deleted from ZKS")
                        return True
                
                logger.warning(f"Cluster deletion initiated but not confirmed within {max_wait}s")
                return True  # Still consider it success as DELETE was accepted
            else:
                logger.info(f"✓ Cluster deleted from ZKS")
                return True
                
        except requests.RequestException as e:
            logger.error(f"Failed to delete cluster from ZKS: {e}")
            return False
    
    def cleanup_cluster(self, cluster_name: str = None, cluster_id: str = None, 
                       kill_agent: bool = True, delete_from_backend: bool = True):
        """
        Clean up a KWOK cluster
        
        Args:
            cluster_name: Name of the cluster to clean up
            cluster_id: Cluster ID in ZKS/Zedcloud (optional, for faster deletion)
            kill_agent: Whether to kill the agent process
            delete_from_backend: Whether to delete from backend (ZKS or Zedcloud)
        """
        logger.info(f"=" * 80)
        logger.info(f"Cleaning up cluster: {cluster_name or cluster_id}")
        logger.info(f"=" * 80)
        
        # Step 1: Delete from backend FIRST (while agent is still connected)
        # This ensures clean deletion before cluster becomes unavailable
        if delete_from_backend:
            # For Zedcloud mode, we need object_id from cluster info file
            if self.import_mode == 'zedcloud' and cluster_name:
                try:
                    info_file = os.path.join('cluster-info', f"{cluster_name}-info.json")
                    if os.path.exists(info_file):
                        with open(info_file, 'r') as f:
                            cluster_info = json.load(f)
                            object_id = cluster_info.get('object_id')
                            if object_id:
                                logger.info(f"Found Zedcloud object_id: {object_id}")
                                self.delete_cluster_from_zedcloud(object_id)
                            else:
                                logger.warning("No object_id found in cluster info for Zedcloud deletion")
                    else:
                        logger.warning(f"Cluster info file not found: {info_file}")
                except Exception as e:
                    logger.warning(f"Failed to read cluster info for Zedcloud deletion: {e}")
            # For ZKS mode, use cluster_id
            elif self.import_mode == 'zks' and cluster_id:
                self.delete_cluster_from_zks(cluster_id)
            elif cluster_id:
                # Fallback: if mode not specified but cluster_id provided, assume ZKS
                logger.info("Import mode not set, assuming ZKS for deletion")
                self.delete_cluster_from_zks(cluster_id)
        
        # Step 2: Stop agent simulator pod (after ZKS deletion initiated)
        if kill_agent and cluster_name:
            try:
                self.cleanup_agent_pod(cluster_name, self.host_kubeconfig)
            except Exception as e:
                logger.warning(f"Failed to delete agent pod: {e}")
        
        # Step 3: Delete KWOK cluster (Docker container)
        if cluster_name:
            try:
                container_name = cluster_name  # Container name is just the cluster name
                # Stop and remove container
                self.run_command(['docker', 'stop', container_name], check=False, capture_output=True)
                self.run_command(['docker', 'rm', container_name], check=False, capture_output=True)
                logger.info(f"✓ KWOK cluster container '{container_name}' deleted")
                
                # Remove kubeconfig file and certificate directory
                kubeconfig_dir = os.path.expanduser(f"~/.kube/kwok-clusters")
                kubeconfig_path = os.path.join(kubeconfig_dir, f"{cluster_name}.yaml")
                cert_dir = os.path.join(kubeconfig_dir, cluster_name)
                
                if os.path.exists(kubeconfig_path):
                    os.remove(kubeconfig_path)
                    logger.info(f"✓ Kubeconfig removed: {kubeconfig_path}")
                
                if os.path.exists(cert_dir):
                    import shutil
                    shutil.rmtree(cert_dir)
                    logger.info(f"✓ Certificate directory removed: {cert_dir}")
            except Exception as e:
                logger.warning(f"Failed to delete KWOK cluster: {e}")
        
        # Step 4: Delete cluster info files and directories
        if cluster_name:
            try:
                # Delete info JSON file
                info_file = os.path.join('cluster-info', f"{cluster_name}-info.json")
                if os.path.exists(info_file):
                    os.remove(info_file)
                    logger.info(f"✓ Deleted info file: {info_file}")
                
                # Delete kubeconfig file
                kubeconfig_file = os.path.join('cluster-info', f"{cluster_name}-kubeconfig.yaml")
                if os.path.exists(kubeconfig_file):
                    os.remove(kubeconfig_file)
                    logger.info(f"✓ Deleted kubeconfig file: {kubeconfig_file}")
                
                # Delete certs directory
                certs_dir = os.path.join('cluster-info', f"{cluster_name}-certs")
                if os.path.exists(certs_dir):
                    import shutil
                    shutil.rmtree(certs_dir)
                    logger.info(f"✓ Deleted certs directory: {certs_dir}")
            except Exception as e:
                logger.warning(f"Failed to delete cluster info files: {e}")
        
        logger.info(f"✓ Cleanup complete")
        logger.info(f"=" * 80)


def main():
    parser = argparse.ArgumentParser(description='KWOK Cluster Simulator for ZKS')
    parser.add_argument('--config', required=True, help='Path to configuration JSON file')
    parser.add_argument('--cluster-name', help='Name for the cluster (default: auto-generated)')
    parser.add_argument('--num-clusters', type=int, default=1, help='Number of clusters to create (default: 1)')
    parser.add_argument('--start-index', type=int, default=1, help='Starting index for cluster numbering (default: 1)')
    parser.add_argument('--num-nodes', type=int, default=1, help='Number of fake nodes per cluster (default: 1)')
    parser.add_argument('--no-agent', action='store_true', help='Do not start agent simulator')
    parser.add_argument('--cleanup', help='Cleanup specified cluster and exit')
    parser.add_argument('--cleanup-all', action='store_true', help='Cleanup all KWOK clusters and exit')
    parser.add_argument('--max-workers', type=int, help='Maximum number of parallel workers for cluster operations (default: auto-detect)')
    parser.add_argument('--no-parallel', action='store_true', help='Disable parallel execution (run sequentially)')
    
    args = parser.parse_args()
    
    # Load configuration
    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)
    
    # Override max_workers from command line if provided
    if args.max_workers:
        config['max_workers'] = args.max_workers
    
    # Determine parallel execution setting early
    use_parallel = not args.no_parallel
    
    # Initialize simulator
    try:
        simulator = KwokClusterSimulator(config)
    except Exception as e:
        logger.error(f"Failed to initialize simulator: {e}")
        sys.exit(1)
    
    # Log parallel execution setting
    if use_parallel:
        logger.info(f"Parallel execution enabled with max_workers={simulator.max_workers}")
    else:
        logger.info("Parallel execution disabled (sequential mode)")
    
    # Cleanup mode
    if args.cleanup:
        cluster_name = args.cleanup
        cluster_id = None
        
        # Try to load cluster info from JSON file
        info_file = os.path.join('cluster-info', f"{cluster_name}-info.json")
        if os.path.exists(info_file):
            try:
                with open(info_file, 'r') as f:
                    cluster_info = json.load(f)
                    cluster_id = cluster_info.get('cluster_id')
                    logger.info(f"Loaded cluster info from {info_file}")
            except Exception as e:
                logger.warning(f"Failed to read {info_file}: {e}")
        
        # Cleanup cluster
        simulator.cleanup_cluster(
            cluster_name=cluster_name,
            cluster_id=cluster_id,
            kill_agent=True,
            delete_from_backend=True if cluster_id else False
        )
        
        # Delete info file if exists
        if os.path.exists(info_file):
            os.remove(info_file)
            logger.info(f"✓ Deleted info file: {info_file}")
        
        return
    
    # Cleanup all mode
    if args.cleanup_all:
        logger.info("Cleaning up all KWOK clusters...")
        import glob
        import subprocess as sp
        
        # First, find all cluster info JSON files
        cluster_info_map = {}
        info_files = glob.glob('cluster-info/*-info.json')
        for info_file in info_files:
            try:
                with open(info_file, 'r') as f:
                    cluster_info = json.load(f)
                    cluster_name = cluster_info.get('name')
                    cluster_id = cluster_info.get('cluster_id')
                    if cluster_name and cluster_id:
                        cluster_info_map[cluster_name] = cluster_id
            except Exception as e:
                logger.warning(f"Failed to read {info_file}: {e}")
        
        # Skip orphan detection in Zedcloud mode (managed by Zedcloud, not direct ZKS access)
        if simulator.import_mode != 'zedcloud':
            # Also find ALL ZKS clusters matching our prefix pattern to catch orphans
            logger.info("Checking for orphaned clusters in ZKS...")
            try:
                result = sp.run(
                    ['kubectl', '--kubeconfig', simulator.zks_kubeconfig_path, 
                     'get', 'clusters.provisioning.cattle.io', '-A', '-o', 'json'],
                    capture_output=True, text=True, check=False
                )
                if result.returncode == 0:
                    import json as json_mod
                    clusters_data = json_mod.loads(result.stdout)
                    for item in clusters_data.get('items', []):
                        cluster_name = item['metadata'].get('name', '')
                        namespace = item['metadata'].get('namespace', '')
                        # Check if this is one of our KWOK clusters
                        display_name = item.get('spec', {}).get('displayName', '')
                        if display_name.startswith(simulator.cluster_name_prefix):
                            if cluster_name not in cluster_info_map.values():
                                logger.info(f"Found orphaned ZKS cluster: {cluster_name} ({display_name})")
                                # Try to delete it
                                sp.run(['kubectl', '--kubeconfig', simulator.zks_kubeconfig_path,
                                       'delete', 'cluster.provisioning.cattle.io', cluster_name, '-n', namespace],
                                      capture_output=True, check=False)
            except Exception as e:
                logger.warning(f"Failed to check for orphaned clusters: {e}")
        
        # Get all KWOK Docker containers (all containers, including those from JSON files)
        all_cluster_names = set(cluster_info_map.keys())
        
        # Find containers by looking for Docker containers (not kwokctl)
        try:
            result = sp.run(
                ['docker', 'ps', '-a', '--filter', 'ancestor=' + simulator.kwok_image, '--format', '{{.Names}}'],
                capture_output=True, text=True, check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                container_clusters = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
                all_cluster_names.update(container_clusters)
        except Exception as e:
            logger.warning(f"Failed to list Docker containers: {e}")
        
        # Cleanup all found clusters (with parallel support)
        if all_cluster_names:
            def cleanup_single_cluster(cluster_name):
                """Helper function to cleanup a single cluster"""
                cluster_id = cluster_info_map.get(cluster_name)
                try:
                    simulator.cleanup_cluster(
                        cluster_name=cluster_name,
                        cluster_id=cluster_id,
                        kill_agent=True,
                        delete_from_backend=True if cluster_id else False
                    )
                    # Delete info files (this is already done in cleanup_cluster, but double-check)
                    info_file = os.path.join('cluster-info', f"{cluster_name}-info.json")
                    if os.path.exists(info_file):
                        os.remove(info_file)
                        logger.info(f"✓ Deleted info file: {info_file}")
                    
                    kubeconfig_file = os.path.join('cluster-info', f"{cluster_name}-kubeconfig.yaml")
                    if os.path.exists(kubeconfig_file):
                        os.remove(kubeconfig_file)
                        logger.info(f"✓ Deleted kubeconfig file: {kubeconfig_file}")
                    
                    certs_dir = os.path.join('cluster-info', f"{cluster_name}-certs")
                    if os.path.exists(certs_dir):
                        import shutil
                        shutil.rmtree(certs_dir)
                        logger.info(f"✓ Deleted certs directory: {certs_dir}")
                    
                    return (cluster_name, True, None)
                except Exception as e:
                    logger.error(f"Failed to cleanup cluster {cluster_name}: {e}")
                    return (cluster_name, False, str(e))
            
            # Use parallel execution if enabled
            if use_parallel and len(all_cluster_names) > 1:
                logger.info(f"Cleaning up {len(all_cluster_names)} clusters in parallel (max_workers={simulator.max_workers})...")
                successful = 0
                failed = 0
                
                with ThreadPoolExecutor(max_workers=simulator.max_workers) as executor:
                    futures = {executor.submit(cleanup_single_cluster, name): name for name in all_cluster_names}
                    
                    for future in as_completed(futures):
                        cluster_name, success, error = future.result()
                        if success:
                            successful += 1
                        else:
                            failed += 1
                
                logger.info(f"\n{'='*80}")
                logger.info(f"✅ Cleanup complete: {successful} successful, {failed} failed")
                logger.info(f"{'='*80}")
            else:
                # Sequential cleanup
                for cluster_name in all_cluster_names:
                    cleanup_single_cluster(cluster_name)
                
                logger.info(f"\n{'='*80}")
                logger.info(f"✅ Cleaned up {len(all_cluster_names)} cluster(s)")
                logger.info(f"{'='*80}")
        else:
            logger.info("No KWOK clusters found")
        
        # Also delete the shared KWOK container if it exists
        logger.info("\nDeleting shared KWOK container...")
        sp.run(['docker', 'rm', '-f', simulator.shared_kwok_name], capture_output=True, check=False)
        logger.info(f"✓ Deleted shared KWOK container: {simulator.shared_kwok_name}")
        
        return
    
    # Initialize shared KWOK cluster (ONE container for all logical clusters)
    try:
        logger.info(f"\n{'='*80}")
        logger.info("Initializing shared KWOK cluster")
        logger.info(f"{'='*80}\n")
        simulator.initialize_shared_kwok()
    except Exception as e:
        logger.error(f"Failed to initialize shared KWOK: {e}")
        sys.exit(1)
    
    # Create multiple logical clusters (with parallel support)
    all_results = []
    
    def create_single_cluster(i, cluster_number):
        """Helper function to create a single cluster"""
        # Generate cluster name: prefix-number (e.g., kwok-scale-test-1)
        if args.cluster_name and args.num_clusters == 1:
            cluster_name = args.cluster_name
        elif args.cluster_name:
            cluster_name = f"{args.cluster_name}-{cluster_number}"
        else:
            cluster_name = f"{simulator.cluster_name_prefix}-{cluster_number}"
        
        try:
            logger.info(f"\n{'='*80}")
            logger.info(f"Creating cluster {i+1}/{args.num_clusters}: {cluster_name}")
            logger.info(f"{'='*80}\n")
            
            result = simulator.create_and_register_cluster(
                cluster_name,
                num_nodes=args.num_nodes,
                start_agent=not args.no_agent
            )
            
            # Save result to file
            output_file = os.path.join('cluster-info', f"{cluster_name}-info.json")
            with open(output_file, 'w') as f:
                json.dump(result, f, indent=2)
            
            logger.info(f"Cluster info saved to: {output_file}")
            return (cluster_name, result, None)
            
        except Exception as e:
            logger.error(f"Failed to create cluster {cluster_name}: {e}")
            return (cluster_name, None, str(e))
    
    # Use parallel execution if enabled and multiple clusters
    if use_parallel and args.num_clusters > 1:
        logger.info(f"Creating {args.num_clusters} clusters in parallel (max_workers={simulator.max_workers})...")
        successful = 0
        failed = 0
        
        with ThreadPoolExecutor(max_workers=simulator.max_workers) as executor:
            futures = {}
            for i in range(args.num_clusters):
                cluster_number = i + args.start_index
                future = executor.submit(create_single_cluster, i, cluster_number)
                futures[future] = cluster_number
            
            for future in as_completed(futures):
                cluster_name, result, error = future.result()
                if result:
                    all_results.append(result)
                    successful += 1
                else:
                    failed += 1
                    if args.num_clusters == 1:
                        sys.exit(1)
        
        logger.info(f"\n{'='*80}")
        logger.info(f"✅ Successfully created {successful}/{args.num_clusters} cluster(s) ({failed} failed)")
        logger.info(f"{'='*80}")
    else:
        # Sequential creation
        for i in range(args.num_clusters):
            cluster_number = i + args.start_index
            cluster_name, result, error = create_single_cluster(i, cluster_number)
            if result:
                all_results.append(result)
            elif args.num_clusters == 1:
                sys.exit(1)
    
    # Summary
    logger.info(f"\n{'='*80}")
    logger.info(f"✅ Successfully created {len(all_results)}/{args.num_clusters} cluster(s)")
    logger.info(f"{'='*80}")
    for result in all_results:
        logger.info(f"  - {result['name']} (ID: {result['cluster_id']})")
    logger.info(f"{'='*80}\n")


if __name__ == "__main__":
    main()
