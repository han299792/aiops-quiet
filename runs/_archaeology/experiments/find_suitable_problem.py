#!/usr/bin/env python3
"""
적합한 문제 찾기 스크립트

목표: mitigation 문제 중에서
- 실험 1번 (E_single): 성공해야 함 (기준 완화 가능)
- 실험 2번 (E_noreset): 실패해야 함

최소한의 실험으로 빠르게 탐색
"""

import asyncio
import time
from typing import List, Dict, Optional
from dataclasses import dataclass

import sys
from pathlib import Path

# 프로젝트 루트를 경로에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from aiopslab.orchestrator.problems.registry import ProblemRegistry
from experiments.reset_comparison_experiment import (
    ResetComparisonExperiment,
    ExperimentConfig,
    RunResult
)


@dataclass
class ProblemTestResult:
    """문제 테스트 결과"""
    problem_id: str
    e_single_success: bool
    e_noreset_attempt1_success: bool
    e_noreset_attempt2_success: bool
    e_single_latency: Optional[float] = None
    e_single_error_rate: Optional[float] = None
    e_noreset_attempt2_latency: Optional[float] = None
    e_noreset_attempt2_error_rate: Optional[float] = None
    e_single_recovery_time: Optional[float] = None
    notes: str = ""


async def quick_test_problem(
    problem_id: str,
    agent_name: str = "gpt",
    latency_threshold: float = 2000.0,  # 완화된 기준
    error_rate_threshold: float = 0.05,  # 5%
    success_window: int = 60,
    timeout: int = 300,  # 짧은 타임아웃으로 빠르게
    max_steps: int = 20
) -> ProblemTestResult:
    """
    문제를 빠르게 테스트 (각 실험 1회만 실행)
    
    Returns:
        ProblemTestResult: 테스트 결과
    """
    print(f"\n{'='*80}")
    print(f"Testing: {problem_id}")
    print(f"{'='*80}")
    
    config = ExperimentConfig(
        problem_id=problem_id,
        agent_name=agent_name,
        latency_threshold=latency_threshold,
        error_rate_threshold=error_rate_threshold,
        success_window=success_window,
        timeout=timeout,
        num_runs=1,  # 1회만 실행
        max_steps=max_steps
    )
    
    experiment = ResetComparisonExperiment(config)
    results: List[RunResult] = []
    
    # 실험 1번 (E_single) 실행
    try:
        print(f"\n[Quick Test] Running E_single (1 run)...")
        e_single_results = await experiment.run_e_single(1)
        results.extend(e_single_results)
        experiment.results.extend(e_single_results)
        
        e_single_result = e_single_results[0] if e_single_results else None
        e_single_success = e_single_result.success if e_single_result else False
        
        print(f"  E_single Result: {'✓ SUCCESS' if e_single_success else '✗ FAILED'}")
        if e_single_result:
            if e_single_result.final_latency_p95:
                print(f"    Latency: {e_single_result.final_latency_p95:.2f} ms")
            if e_single_result.final_error_rate is not None:
                print(f"    Error Rate: {e_single_result.final_error_rate*100:.2f}%")
            if e_single_result.failure_reason:
                print(f"    Reason: {e_single_result.failure_reason}")
        
        # 실험 1번이 실패하면 더 이상 진행하지 않음
        if not e_single_success:
            return ProblemTestResult(
                problem_id=problem_id,
                e_single_success=False,
                e_noreset_attempt1_success=False,
                e_noreset_attempt2_success=False,
                notes="E_single failed, skipping E_noreset"
            )
        
    except Exception as e:
        print(f"  ERROR in E_single: {e}")
        return ProblemTestResult(
            problem_id=problem_id,
            e_single_success=False,
            e_noreset_attempt1_success=False,
            e_noreset_attempt2_success=False,
            notes=f"E_single error: {str(e)}"
        )
    
    # 실험 2번 (E_noreset) 실행
    try:
        print(f"\n[Quick Test] Running E_noreset (1 run)...")
        e_noreset_results = await experiment.run_e_noreset(1)
        results.extend(e_noreset_results)
        experiment.results.extend(e_noreset_results)
        
        attempt1_result = next((r for r in e_noreset_results if r.incident_attempt == 1), None)
        attempt2_result = next((r for r in e_noreset_results if r.incident_attempt == 2), None)
        
        attempt1_success = attempt1_result.success if attempt1_result else False
        attempt2_success = attempt2_result.success if attempt2_result else False
        
        print(f"  E_noreset Attempt 1: {'✓ SUCCESS' if attempt1_success else '✗ FAILED'}")
        print(f"  E_noreset Attempt 2: {'✓ SUCCESS' if attempt2_success else '✗ FAILED'}")
        
        if attempt2_result:
            if attempt2_result.final_latency_p95:
                print(f"    Attempt 2 Latency: {attempt2_result.final_latency_p95:.2f} ms")
            if attempt2_result.final_error_rate is not None:
                print(f"    Attempt 2 Error Rate: {attempt2_result.final_error_rate*100:.2f}%")
            if attempt2_result.failure_reason:
                print(f"    Attempt 2 Reason: {attempt2_result.failure_reason}")
        
        return ProblemTestResult(
            problem_id=problem_id,
            e_single_success=e_single_success,
            e_noreset_attempt1_success=attempt1_success,
            e_noreset_attempt2_success=attempt2_success,
            e_single_latency=e_single_result.final_latency_p95 if e_single_result else None,
            e_single_error_rate=e_single_result.final_error_rate if e_single_result else None,
            e_noreset_attempt2_latency=attempt2_result.final_latency_p95 if attempt2_result else None,
            e_noreset_attempt2_error_rate=attempt2_result.final_error_rate if attempt2_result else None,
            e_single_recovery_time=e_single_result.recovery_time if e_single_result else None,
            notes=""
        )
        
    except Exception as e:
        print(f"  ERROR in E_noreset: {e}")
        return ProblemTestResult(
            problem_id=problem_id,
            e_single_success=e_single_success,
            e_noreset_attempt1_success=False,
            e_noreset_attempt2_success=False,
            notes=f"E_noreset error: {str(e)}"
        )


async def find_suitable_problems(
    agent_name: str = "gpt",
    latency_threshold: float = 2000.0,
    error_rate_threshold: float = 0.05,
    problem_ids: Optional[List[str]] = None
):
    """
    적합한 문제 찾기
    
    조건:
    - E_single: 성공
    - E_noreset Attempt 2: 실패
    """
    registry = ProblemRegistry()
    
    # mitigation 문제들만 필터링
    if problem_ids is None:
        all_problems = registry.get_problem_ids()
        mitigation_problems = [p for p in all_problems if "mitigation" in p]
    else:
        mitigation_problems = [p for p in problem_ids if "mitigation" in p]
    
    print(f"\n{'='*80}")
    print(f"Finding Suitable Problems")
    print(f"{'='*80}")
    print(f"Total mitigation problems: {len(mitigation_problems)}")
    print(f"Agent: {agent_name}")
    print(f"Thresholds: Latency < {latency_threshold}ms, Error Rate < {error_rate_threshold*100:.1f}%")
    print(f"{'='*80}\n")
    
    suitable_problems: List[ProblemTestResult] = []
    tested_problems: List[ProblemTestResult] = []
    
    for i, problem_id in enumerate(mitigation_problems, 1):
        print(f"\n[{i}/{len(mitigation_problems)}] Testing: {problem_id}")
        
        try:
            result = await quick_test_problem(
                problem_id=problem_id,
                agent_name=agent_name,
                latency_threshold=latency_threshold,
                error_rate_threshold=error_rate_threshold,
                timeout=300,  # 5분 타임아웃
                max_steps=20
            )
            
            tested_problems.append(result)
            
            # 조건 확인: E_single 성공 AND E_noreset Attempt 2 실패
            if result.e_single_success and not result.e_noreset_attempt2_success:
                suitable_problems.append(result)
                print(f"\n{'='*80}")
                print(f"✓ SUITABLE PROBLEM FOUND: {problem_id}")
                print(f"{'='*80}")
                print(f"  E_single: SUCCESS")
                print(f"  E_noreset Attempt 2: FAILED (as desired)")
                if result.e_single_latency:
                    print(f"  E_single Latency: {result.e_single_latency:.2f} ms")
                if result.e_single_error_rate is not None:
                    print(f"  E_single Error Rate: {result.e_single_error_rate*100:.2f}%")
                print(f"{'='*80}\n")
            
            # Run 간 대기
            if i < len(mitigation_problems):
                print(f"\nWaiting 10 seconds before next test...")
                time.sleep(10)
                
        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
            break
        except Exception as e:
            print(f"  ERROR testing {problem_id}: {e}")
            tested_problems.append(ProblemTestResult(
                problem_id=problem_id,
                e_single_success=False,
                e_noreset_attempt1_success=False,
                e_noreset_attempt2_success=False,
                notes=f"Test error: {str(e)}"
            ))
            continue
    
    # 최종 결과 출력
    print(f"\n{'='*80}")
    print("FINAL RESULTS")
    print(f"{'='*80}\n")
    
    print(f"Total tested: {len(tested_problems)}")
    print(f"Suitable problems found: {len(suitable_problems)}\n")
    
    if suitable_problems:
        print("SUITABLE PROBLEMS (E_single ✓, E_noreset Attempt 2 ✗):")
        print("-" * 80)
        for result in suitable_problems:
            print(f"\n{result.problem_id}:")
            print(f"  E_single: SUCCESS")
            print(f"  E_noreset Attempt 1: {'SUCCESS' if result.e_noreset_attempt1_success else 'FAILED'}")
            print(f"  E_noreset Attempt 2: FAILED (desired)")
            if result.e_single_latency:
                print(f"  E_single Latency: {result.e_single_latency:.2f} ms")
            if result.e_single_error_rate is not None:
                print(f"  E_single Error Rate: {result.e_single_error_rate*100:.2f}%")
            if result.e_single_recovery_time:
                print(f"  E_single Recovery Time: {result.e_single_recovery_time:.2f}s")
    else:
        print("No suitable problems found.")
        print("\nTested problems summary:")
        print("-" * 80)
        for result in tested_problems:
            status = []
            if result.e_single_success:
                status.append("E1✓")
            else:
                status.append("E1✗")
            if result.e_noreset_attempt2_success:
                status.append("E2✓")
            else:
                status.append("E2✗")
            print(f"  {result.problem_id}: {' '.join(status)} {result.notes}")
    
    print(f"\n{'='*80}")


async def main():
    """메인 함수"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Find suitable problems for reset comparison experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
목표: mitigation 문제 중에서
- 실험 1번 (E_single): 성공해야 함
- 실험 2번 (E_noreset) Attempt 2: 실패해야 함

예시:
  # 모든 mitigation 문제 테스트
  python3 experiments/find_suitable_problem.py --agent gpt
  
  # 특정 문제들만 테스트
  python3 experiments/find_suitable_problem.py --agent gpt --problem-ids misconfig_app_hotel_res-mitigation-1 pod_kill_hotel_res-mitigation-1
  
  # 기준 완화
  python3 experiments/find_suitable_problem.py --agent gpt --latency-threshold 3000 --error-rate-threshold 0.1
        """
    )
    parser.add_argument("--agent", type=str, default="gpt",
                       help="Agent name (default: gpt)")
    parser.add_argument("--latency-threshold", type=float, default=2000.0,
                       help="Latency threshold in ms (default: 2000.0)")
    parser.add_argument("--error-rate-threshold", type=float, default=0.05,
                       help="Error rate threshold (default: 0.05 = 5%%)")
    parser.add_argument("--problem-ids", nargs="+",
                       help="Specific problem IDs to test (default: all mitigation problems)")
    parser.add_argument("--success-window", type=int, default=60,
                       help="Success window in seconds (default: 60)")
    parser.add_argument("--timeout", type=int, default=300,
                       help="Timeout in seconds (default: 300)")
    parser.add_argument("--max-steps", type=int, default=20,
                       help="Max steps for agent (default: 20)")
    
    args = parser.parse_args()
    
    await find_suitable_problems(
        agent_name=args.agent,
        latency_threshold=args.latency_threshold,
        error_rate_threshold=args.error_rate_threshold,
        problem_ids=args.problem_ids
    )


if __name__ == "__main__":
    asyncio.run(main())
