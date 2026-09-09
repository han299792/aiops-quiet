#!/usr/bin/env python3
"""
Pod 상태 디버깅 스크립트

특정 namespace의 pod 상태를 확인하고 문제를 진단합니다.
"""

import sys
from aiopslab.service.kubectl import KubeCtl


def debug_namespace(namespace: str):
    """Namespace의 pod 상태를 상세히 출력"""
    kubectl = KubeCtl()
    
    print(f"\n{'='*60}")
    print(f"Debugging namespace: {namespace}")
    print(f"{'='*60}\n")
    
    try:
        pod_list = kubectl.list_pods(namespace)
        
        if not pod_list.items:
            print(f"[WARNING] No pods found in namespace '{namespace}'")
            return
        
        print(f"Found {len(pod_list.items)} pods:\n")
        
        for pod in pod_list.items:
            pod_name = pod.metadata.name
            phase = pod.status.phase
            print(f"Pod: {pod_name}")
            print(f"  Phase: {phase}")
            print(f"  Node: {pod.spec.node_name if pod.spec.node_name else 'N/A'}")
            
            if pod.status.container_statuses:
                print(f"  Containers ({len(pod.status.container_statuses)}):")
                for cs in pod.status.container_statuses:
                    print(f"    - {cs.name}:")
                    print(f"        Ready: {cs.ready}")
                    
                    if cs.state.waiting:
                        print(f"        State: Waiting")
                        print(f"        Reason: {cs.state.waiting.reason}")
                        if cs.state.waiting.message:
                            print(f"        Message: {cs.state.waiting.message}")
                    elif cs.state.running:
                        print(f"        State: Running")
                        print(f"        Started: {cs.state.running.started_at}")
                    elif cs.state.terminated:
                        print(f"        State: Terminated")
                        print(f"        Reason: {cs.state.terminated.reason}")
                        if cs.state.terminated.message:
                            print(f"        Message: {cs.state.terminated.message}")
                        if cs.state.terminated.exit_code is not None:
                            print(f"        Exit Code: {cs.state.terminated.exit_code}")
                    
                    # Restart count
                    if cs.restart_count > 0:
                        print(f"        Restart Count: {cs.restart_count}")
            else:
                print(f"  [WARNING] No container statuses available")
            
            # Pod conditions
            if pod.status.conditions:
                print(f"  Conditions:")
                for condition in pod.status.conditions:
                    print(f"    - {condition.type}: {condition.status}")
                    if condition.reason:
                        print(f"      Reason: {condition.reason}")
                    if condition.message:
                        print(f"      Message: {condition.message}")
            
            # Events (최근)
            try:
                events = kubectl.core_v1_api.list_namespaced_event(
                    namespace=namespace,
                    field_selector=f"involvedObject.name={pod_name}"
                )
                if events.items:
                    print(f"  Recent Events:")
                    for event in sorted(events.items, key=lambda x: x.last_timestamp, reverse=True)[:3]:
                        print(f"    - {event.type}: {event.reason} - {event.message}")
            except:
                pass
            
            print()
        
        # Ready 상태 요약
        ready_pods = [
            pod for pod in pod_list.items
            if pod.status.container_statuses and
            all(cs.ready for cs in pod.status.container_statuses)
        ]
        
        print(f"\n{'='*60}")
        print(f"Summary: {len(ready_pods)}/{len(pod_list.items)} pods are ready")
        print(f"{'='*60}\n")
        
        if len(ready_pods) < len(pod_list.items):
            not_ready = [p for p in pod_list.items if p not in ready_pods]
            print("Not ready pods:")
            for pod in not_ready:
                print(f"  - {pod.metadata.name}: {pod.status.phase}")
                if pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        if not cs.ready:
                            if cs.state.waiting:
                                print(f"      {cs.name}: Waiting - {cs.state.waiting.reason}")
                            elif cs.state.terminated:
                                print(f"      {cs.name}: Terminated - {cs.state.terminated.reason}")
        
    except Exception as e:
        print(f"[ERROR] Failed to get pod status: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 debug_pods.py <namespace>")
        print("Example: python3 debug_pods.py test-hotel-reservation")
        sys.exit(1)
    
    namespace = sys.argv[1]
    debug_namespace(namespace)
