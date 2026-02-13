"""
Zedcloud API Client for cluster import and management.

This client wraps Zedcloud REST API endpoints for:
- Importing clusters
- Getting registration commands/manifests
- Checking cluster status
"""

import requests
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ZedcloudClient:
    """Client for Zedcloud API operations."""
    
    def __init__(self, base_url: str, token: str):
        """
        Initialize Zedcloud API client.
        
        Args:
            base_url: Zedcloud base URL (e.g., https://zedcontrol.thor.zededa.dev)
            token: Bearer token in format "username:secret"
        """
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
    
    def get_project_id_by_name(self, project_name: str) -> str:
        """
        Get project ID by project name.
        
        Args:
            project_name: Name of the project
            
        Returns:
            Project ID (UUID)
            
        Raises:
            requests.HTTPError: If request fails
            ValueError: If project not found
        """
        url = f"{self.base_url}/api/v1/projects"
        
        logger.debug(f"Looking up project ID for '{project_name}'...")
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        
        data = response.json()
        projects = data.get('list', [])
        
        # Find project by name
        for project in projects:
            if project.get('name') == project_name:
                project_id = project.get('id')
                logger.info(f"Found project '{project_name}' with ID: {project_id}")
                return project_id
        
        raise ValueError(f"Project '{project_name}' not found in Zedcloud")
    
    def import_cluster(
        self, 
        name: str, 
        title: str, 
        project_name: str,
        description: str = "",
        tags: Optional[Dict] = None
    ) -> str:
        """
        Import a cluster into Zedcloud.
        
        Args:
            name: Cluster name (unique identifier)
            title: Display title for the cluster
            project_name: Zedcloud project name (will be resolved to ID)
            description: Optional cluster description
            tags: Optional key-value tags
            
        Returns:
            objectId: Zedcloud cluster object ID (UUID)
            
        Raises:
            requests.HTTPError: If import fails
            ValueError: If project not found
        """
        # Lookup project ID from name
        project_id = self.get_project_id_by_name(project_name)
        
        url = f"{self.base_url}/api/v1/zks/instances/import"
        payload = {
            "name": name,
            "title": title,
            "description": description,
            "projectId": project_id,
            "tags": tags or {}
        }
        
        logger.info(f"Importing cluster '{name}' to Zedcloud...")
        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()
        
        data = response.json()
        object_id = data.get('objectId')
        operation_status = data.get('operationStatus')
        
        if operation_status != 'OPS_STATUS_COMPLETE':
            raise RuntimeError(
                f"Import operation not complete: {operation_status}"
            )
        
        logger.info(f"Cluster '{name}' imported successfully. ObjectID: {object_id}")
        return object_id
    
    def get_registration_commands(self, object_id: str) -> Dict[str, str]:
        """
        Get registration commands and manifest URL for a cluster.
        
        Args:
            object_id: Zedcloud cluster object ID
            
        Returns:
            Dictionary with:
                - manifest_url: URL to the registration YAML manifest
                - command: kubectl apply command
                - insecure_command: curl | kubectl command for air-gapped
                
        Raises:
            requests.HTTPError: If request fails
        """
        url = f"{self.base_url}/api/v1/zks/instances/id/{object_id}/registration-commands"
        
        logger.info(f"Getting registration commands for cluster {object_id}...")
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        
        data = response.json()
        return {
            'manifest_url': data['manifestUrl'],
            'command': data['commands']['command'],
            'insecure_command': data['commands']['insecureCommand']
        }
    
    def get_cluster_status(self, cluster_name: str) -> Dict:
        """
        Get cluster status by name.
        
        Args:
            cluster_name: Name of the cluster
            
        Returns:
            Dictionary with cluster status including:
                - id: Zedcloud object ID
                - zksClusterId: Fleet cluster ID (e.g., "c-5t9q5")
                - runState: Cluster run state
                - isImported: Whether cluster is imported
                - nOfNodes: Number of nodes
                - capacity: Resource capacity metrics
                
        Raises:
            requests.HTTPError: If request fails
            ValueError: If cluster not found
        """
        url = f"{self.base_url}/api/v1/zks/instances/status"
        params = {
            'zksname': cluster_name,
            'next.orderBy': 'name:ASC'
        }
        
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        
        data = response.json()
        if not data.get('list') or len(data['list']) == 0:
            raise ValueError(f"Cluster '{cluster_name}' not found")
        
        return data['list'][0]
    
    def wait_for_cluster_ready(
        self, 
        cluster_name: str, 
        timeout: int = 300,
        check_interval: int = 10
    ) -> bool:
        """
        Wait for cluster to transition from UNSPECIFIED state.
        
        Args:
            cluster_name: Name of the cluster
            timeout: Maximum time to wait in seconds
            check_interval: Time between status checks in seconds
            
        Returns:
            True if cluster becomes ready, False if timeout
        """
        import time
        
        start_time = time.time()
        logger.info(f"Waiting for cluster '{cluster_name}' to be ready...")
        
        while time.time() - start_time < timeout:
            try:
                status = self.get_cluster_status(cluster_name)
                run_state = status.get('runState')
                
                if run_state and run_state != 'RUN_STATE_UNSPECIFIED':
                    logger.info(
                        f"Cluster '{cluster_name}' is ready. State: {run_state}"
                    )
                    return True
                
                elapsed = int(time.time() - start_time)
                logger.debug(
                    f"Cluster '{cluster_name}' still in UNSPECIFIED state "
                    f"({elapsed}/{timeout}s)..."
                )
                
            except Exception as e:
                logger.warning(f"Error checking cluster status: {e}")
            
            time.sleep(check_interval)
        
        logger.error(
            f"Cluster '{cluster_name}' not ready after {timeout}s timeout"
        )
        return False
    
    def delete_cluster(self, object_id: str) -> bool:
        """
        Delete a cluster from Zedcloud.
        
        Args:
            object_id: Zedcloud cluster object ID (UUID)
            
        Returns:
            True if deletion successful, False otherwise
            
        Raises:
            requests.HTTPError: If request fails
        """
        url = f"{self.base_url}/api/v1/zks/instances/id/{object_id}"
        
        logger.info(f"Deleting cluster with object_id '{object_id}' from Zedcloud...")
        response = requests.delete(url, headers=self.headers)
        response.raise_for_status()
        
        data = response.json()
        operation_status = data.get('operationStatus')
        object_name = data.get('objectName', 'unknown')
        
        if operation_status != 'OPS_STATUS_COMPLETE':
            logger.error(
                f"Delete operation not complete: {operation_status}"
            )
            return False
        
        logger.info(f"✓ Cluster '{object_name}' (ID: {object_id}) deleted from Zedcloud")
        return True
