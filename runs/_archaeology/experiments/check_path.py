#!/usr/bin/env python3
"""
경로 확인 스크립트

hotel reservation의 Kubernetes 배포 경로가 올바른지 확인합니다.
"""

import sys
from pathlib import Path
from aiopslab.paths import TARGET_MICROSERVICES, HOTEL_RES_METADATA

def check_hotel_reservation_path():
    """Hotel reservation의 경로를 확인"""
    print(f"\n{'='*60}")
    print("Checking Hotel Reservation Kubernetes Deployment Path")
    print(f"{'='*60}\n")
    
    print(f"TARGET_MICROSERVICES: {TARGET_MICROSERVICES}")
    print(f"  Exists: {TARGET_MICROSERVICES.exists()}")
    print(f"  Is directory: {TARGET_MICROSERVICES.is_dir() if TARGET_MICROSERVICES.exists() else 'N/A'}\n")
    
    # Load metadata
    import json
    with open(HOTEL_RES_METADATA, 'r') as f:
        metadata = json.load(f)
    
    k8s_deploy_path = metadata.get("K8S Deploy Path")
    print(f"K8S Deploy Path (from metadata): {k8s_deploy_path}")
    
    if k8s_deploy_path:
        full_path = TARGET_MICROSERVICES / k8s_deploy_path
        print(f"Full path: {full_path}")
        print(f"  Exists: {full_path.exists()}")
        
        if full_path.exists():
            if full_path.is_dir():
                print(f"  Is directory: True")
                yaml_files = list(full_path.rglob("*.yaml")) + list(full_path.rglob("*.yml"))
                print(f"  YAML files found: {len(yaml_files)}")
                
                if yaml_files:
                    print(f"\n  YAML files:")
                    for yaml_file in yaml_files[:20]:  # Show first 20
                        rel_path = yaml_file.relative_to(full_path)
                        print(f"    - {rel_path}")
                    if len(yaml_files) > 20:
                        print(f"    ... and {len(yaml_files) - 20} more files")
                else:
                    print(f"  [WARNING] No YAML files found!")
            elif full_path.is_file():
                print(f"  Is file: True")
            else:
                print(f"  [ERROR] Path exists but is neither file nor directory")
        else:
            print(f"  [ERROR] Path does not exist!")
            print(f"\n  Checking parent directories:")
            current = full_path.parent
            while current != TARGET_MICROSERVICES.parent:
                print(f"    {current}: {'exists' if current.exists() else 'NOT EXISTS'}")
                if current.exists():
                    if current.is_dir():
                        contents = list(current.iterdir())[:10]
                        print(f"      Contents: {[c.name for c in contents]}")
                    break
                current = current.parent
    
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    try:
        check_hotel_reservation_path()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
