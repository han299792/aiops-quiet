#!/usr/bin/env python3
"""
Deployment 상태 디버깅 스크립트

특정 namespace의 모든 리소스 상태를 확인하고 문제를 진단합니다.
"""

import sys
from aiopslab.service.kubectl import KubeCtl


def debug_namespace_resources(namespace: str):
    """Namespace의 모든 리소스 상태를 확인"""
    kubectl = KubeCtl()
    
    print(f"\n{'='*60}")
    print(f"Debugging namespace: {namespace}")
    print(f"{'='*60}\n")
    
    # Namespace 존재 확인
    try:
        ns = kubectl.core_v1_api.read_namespace(name=namespace)
        print(f"✓ Namespace '{namespace}' exists")
        print(f"  Status: {ns.status.phase}")
    except Exception as e:
        print(f"✗ Namespace '{namespace}' does not exist or cannot be accessed: {e}")
        return
    
    # Pods 확인
    print(f"\n--- Pods ---")
    try:
        pod_list = kubectl.list_pods(namespace)
        print(f"Found {len(pod_list.items)} pods")
        if pod_list.items:
            for pod in pod_list.items:
                print(f"  - {pod.metadata.name}: {pod.status.phase}")
        else:
            print("  [WARNING] No pods found")
    except Exception as e:
        print(f"  [ERROR] Failed to list pods: {e}")
    
    # Deployments 확인
    print(f"\n--- Deployments ---")
    try:
        deployments = kubectl.apps_v1_api.list_namespaced_deployment(namespace)
        print(f"Found {len(deployments.items)} deployments")
        for deploy in deployments.items:
            print(f"  - {deploy.metadata.name}:")
            print(f"      Desired: {deploy.spec.replicas}")
            print(f"      Ready: {deploy.status.ready_replicas if deploy.status.ready_replicas else 0}")
            print(f"      Available: {deploy.status.available_replicas if deploy.status.available_replicas else 0}")
            if deploy.status.conditions:
                for condition in deploy.status.conditions:
                    if condition.status != "True":
                        print(f"      Condition {condition.type}: {condition.status} - {condition.reason}")
                        if condition.message:
                            print(f"        Message: {condition.message}")
    except Exception as e:
        print(f"  [ERROR] Failed to list deployments: {e}")
    
    # StatefulSets 확인
    print(f"\n--- StatefulSets ---")
    try:
        statefulsets = kubectl.apps_v1_api.list_namespaced_stateful_set(namespace)
        print(f"Found {len(statefulsets.items)} statefulsets")
        for sts in statefulsets.items:
            print(f"  - {sts.metadata.name}:")
            print(f"      Desired: {sts.spec.replicas}")
            print(f"      Ready: {sts.status.ready_replicas if sts.status.ready_replicas else 0}")
            print(f"      Current: {sts.status.current_replicas if sts.status.current_replicas else 0}")
    except Exception as e:
        print(f"  [ERROR] Failed to list statefulsets: {e}")
    
    # ReplicaSets 확인
    print(f"\n--- ReplicaSets ---")
    try:
        replicasets = kubectl.apps_v1_api.list_namespaced_replica_set(namespace)
        print(f"Found {len(replicasets.items)} replicasets")
        for rs in replicasets.items:
            print(f"  - {rs.metadata.name}:")
            print(f"      Desired: {rs.spec.replicas}")
            print(f"      Ready: {rs.status.ready_replicas if rs.status.ready_replicas else 0}")
            if rs.status.conditions:
                for condition in rs.status.conditions:
                    if condition.status != "True":
                        print(f"      Condition {condition.type}: {condition.status} - {condition.reason}")
    except Exception as e:
        print(f"  [ERROR] Failed to list replicasets: {e}")
    
    # Services 확인
    print(f"\n--- Services ---")
    try:
        services = kubectl.list_services(namespace)
        print(f"Found {len(services.items)} services")
        for svc in services.items:
            print(f"  - {svc.metadata.name}: {svc.spec.type}")
    except Exception as e:
        print(f"  [ERROR] Failed to list services: {e}")
    
    # ConfigMaps 확인
    print(f"\n--- ConfigMaps ---")
    try:
        configmaps = kubectl.core_v1_api.list_namespaced_config_map(namespace)
        print(f"Found {len(configmaps.items)} configmaps")
        for cm in configmaps.items:
            print(f"  - {cm.metadata.name}")
    except Exception as e:
        print(f"  [ERROR] Failed to list configmaps: {e}")
    
    # PVCs 확인
    print(f"\n--- PersistentVolumeClaims ---")
    try:
        pvcs = kubectl.core_v1_api.list_namespaced_persistent_volume_claim(namespace)
        print(f"Found {len(pvcs.items)} PVCs")
        for pvc in pvcs.items:
            print(f"  - {pvc.metadata.name}: {pvc.status.phase}")
            if pvc.status.phase != "Bound":
                print(f"      [WARNING] PVC is not bound")
    except Exception as e:
        print(f"  [ERROR] Failed to list PVCs: {e}")
    
    # Events 확인 (최근)
    print(f"\n--- Recent Events ---")
    try:
        events = kubectl.core_v1_api.list_namespaced_event(
            namespace=namespace,
            limit=20
        )
        if events.items:
            print(f"Found {len(events.items)} recent events:")
            for event in sorted(events.items, key=lambda x: x.last_timestamp, reverse=True)[:10]:
                print(f"  - [{event.type}] {event.reason}: {event.message}")
                print(f"    Object: {event.involved_object.kind}/{event.involved_object.name}")
        else:
            print("  No events found")
    except Exception as e:
        print(f"  [ERROR] Failed to list events: {e}")
    
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 debug_deployment.py <namespace>")
        print("Example: python3 debug_deployment.py test-hotel-reservation")
        sys.exit(1)
    
    namespace = sys.argv[1]
    debug_namespace_resources(namespace)
