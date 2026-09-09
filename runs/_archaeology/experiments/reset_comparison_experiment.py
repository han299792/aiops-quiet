#!/usr/bin/env python3
"""
Reset Comparison Experiment

비교 실험:
- E_single: 각 run마다 reset 후 incident A 1회 실행
- E_noreset: reset 1회만 하고 incident A를 2회 연속 실행 (중간 reset 없음)

판정 기준:
- 성공: p95 latency < L AND error_rate < e가 연속 W초 유지
- 실패: timeout(T초) 내 성공 조건 미달
"""

import asyncio
import time
import json
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict
from pathlib import Path

from aiopslab.orchestrator.orchestrator import Orchestrator
from aiopslab.orchestrator.problems.registry import ProblemRegistry
from aiopslab.observer.metric_api import PrometheusAPI
from aiopslab.observer import monitor_config
from aiopslab.clients.registry import AgentRegistry


@dataclass
class ExperimentConfig:
    """실험 설정"""
    # Incident 설정
    problem_id: str  # 예: "pod_kill_hotel_res-detection-1" 또는 "network_delay_hotel_res-detection-1"
    agent_name: str  # 예: "gpt" 또는 다른 agent
    
    # 판정 기준
    latency_threshold: float = 1000.0  # L: p95 latency 임계값 (ms)
    error_rate_threshold: float = 0.01  # e: error rate 임계값 (1% = 0.01)
    success_window: int = 60  # W: 연속 성공 유지 시간 (초)
    timeout: int = 600  # T: 타임아웃 (초)
    
    # 실험 설정
    num_runs: int = 5  # 각 조건당 실행 횟수
    max_steps: int = 20  # Agent 최대 스텝 수
    
    # 결과 저장 경로
    results_dir: str = "experiments/results"


@dataclass
class RunResult:
    """단일 실행 결과"""
    run_id: int
    condition: str  # "E_single" or "E_noreset"
    incident_attempt: int  # 1 또는 2 (E_noreset의 경우)
    success: bool
    recovery_time: Optional[float]  # 복구 시간 (초)
    final_latency_p95: Optional[float]
    final_error_rate: Optional[float]
    failure_reason: Optional[str]
    timestamp: str


class MetricMonitor:
    """메트릭 모니터링 및 판정"""
    
    def __init__(self, namespace: str, latency_threshold: float, error_rate_threshold: float):
        self.namespace = namespace
        self.latency_threshold = latency_threshold
        self.error_rate_threshold = error_rate_threshold
        self.prometheus = None
        
        try:
            self.prometheus = PrometheusAPI(
                monitor_config["prometheusApi"],
                namespace
            )
        except Exception as e:
            print(f"Warning: Failed to initialize Prometheus API: {e}")
    
    def check_success_condition(self, duration: int = 5) -> Tuple[bool, Optional[float], Optional[float]]:
        """
        성공 조건 체크: p95 latency < L AND error_rate < e
        
        Returns:
            (is_success, p95_latency, error_rate)
        """
        if not self.prometheus:
            return False, None, None
        
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(minutes=duration)
            
            # p95 latency 쿼리 (Istio 메트릭 사용)
            latency_query = f'histogram_quantile(0.95, sum(rate(istio_request_duration_milliseconds_bucket{{namespace="{self.namespace}"}}[1m])) by (le))'
            latency_result = self.prometheus.client.custom_query(latency_query)
            
            p95_latency = None
            if latency_result and len(latency_result) > 0:
                if "value" in latency_result[0] and len(latency_result[0]["value"]) >= 2:
                    p95_latency = float(latency_result[0]["value"][1])
            
            # Error rate 쿼리
            error_query = f'sum(rate(istio_requests_total{{namespace="{self.namespace}",response_code=~"5.."}}[1m])) / sum(rate(istio_requests_total{{namespace="{self.namespace}"}}[1m]))'
            error_result = self.prometheus.client.custom_query(error_query)
            
            error_rate = None
            if error_result and len(error_result) > 0:
                if "value" in error_result[0] and len(error_result[0]["value"]) >= 2:
                    error_rate = float(error_result[0]["value"][1])
            
            if p95_latency is None or error_rate is None:
                return False, p95_latency, error_rate
            
            is_success = (p95_latency < self.latency_threshold and 
                         error_rate < self.error_rate_threshold)
            
            return is_success, p95_latency, error_rate
            
        except Exception as e:
            print(f"Error checking metrics: {e}")
            return False, None, None
    
    def wait_for_success(self, window: int, timeout: int) -> Tuple[bool, Optional[float], Optional[float], Optional[str]]:
        """
        연속 W초 동안 성공 조건 유지 대기
        
        Returns:
            (success, final_latency, final_error_rate, failure_reason)
        """
        start_time = time.time()
        success_start_time = None
        check_interval = 5  # 5초마다 체크
        
        while time.time() - start_time < timeout:
            is_success, latency, error_rate = self.check_success_condition()
            
            if is_success:
                if success_start_time is None:
                    success_start_time = time.time()
                elif time.time() - success_start_time >= window:
                    # 연속 W초 동안 성공 조건 만족
                    return True, latency, error_rate, None
            else:
                success_start_time = None  # 리셋
            
            time.sleep(check_interval)
        
        # Timeout
        _, final_latency, final_error_rate = self.check_success_condition()
        return False, final_latency, final_error_rate, f"Timeout after {timeout}s"


class ResetComparisonExperiment:
    """Reset 비교 실험 실행"""
    
    def __init__(self, config: ExperimentConfig):
        self.config = config
        self.results: List[RunResult] = []
        self.results_dir = Path(config.results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
    
    async def run_single_incident(self, orchestrator: Orchestrator, 
                                   condition: str, incident_attempt: int) -> RunResult:
        """단일 incident 실행"""
        print(f"\n{'='*60}")
        print(f"Running {condition} - Incident Attempt {incident_attempt}")
        print(f"{'='*60}")
        
        start_time = time.time()
        monitor = MetricMonitor(
            namespace=orchestrator.session.problem.namespace,
            latency_threshold=self.config.latency_threshold,
            error_rate_threshold=self.config.error_rate_threshold
        )
        
        try:
            # Agent 실행
            await orchestrator.start_problem(max_steps=self.config.max_steps)
            
            # 복구 후 성공 조건 체크
            recovery_time = time.time() - start_time
            success, final_latency, final_error_rate, failure_reason = monitor.wait_for_success(
                window=self.config.success_window,
                timeout=self.config.timeout
            )
            
            result = RunResult(
                run_id=len(self.results) + 1,
                condition=condition,
                incident_attempt=incident_attempt,
                success=success,
                recovery_time=recovery_time,
                final_latency_p95=final_latency,
                final_error_rate=final_error_rate,
                failure_reason=failure_reason,
                timestamp=datetime.now().isoformat()
            )
            
            print(f"\nResult: {'SUCCESS' if success else 'FAILURE'}")
            if final_latency:
                print(f"  p95 Latency: {final_latency:.2f} ms (threshold: {self.config.latency_threshold} ms)")
            if final_error_rate is not None:
                print(f"  Error Rate: {final_error_rate*100:.2f}% (threshold: {self.config.error_rate_threshold*100}%)")
            if failure_reason:
                print(f"  Failure Reason: {failure_reason}")
            
            return result
            
        except Exception as e:
            print(f"Error during incident execution: {e}")
            return RunResult(
                run_id=len(self.results) + 1,
                condition=condition,
                incident_attempt=incident_attempt,
                success=False,
                recovery_time=time.time() - start_time,
                final_latency_p95=None,
                final_error_rate=None,
                failure_reason=str(e),
                timestamp=datetime.now().isoformat()
            )
    
    async def run_e_single(self, run_id: int) -> List[RunResult]:
        """E_single: 각 run마다 reset 후 incident 1회 실행"""
        results = []
        
        # Agent 및 Orchestrator 초기화
        agent_registry = AgentRegistry()
        agent_cls = agent_registry.get_agent(self.config.agent_name)
        if agent_cls is None:
            raise ValueError(f"Unknown agent: {self.config.agent_name}")
        
        agent = agent_cls()
        orchestrator = Orchestrator()
        orchestrator.register_agent(agent, name=f"{self.config.agent_name}-agent")
        
        # Reset: 문제 초기화
        print(f"\n[E_single Run {run_id}] Resetting environment...")
        try:
            problem_desc, instructs, apis = orchestrator.init_problem(self.config.problem_id)
            agent.init_context(problem_desc, instructs, apis)
        except Exception as e:
            print(f"[ERROR] Failed to initialize problem: {e}")
            # Pod 상태 확인
            try:
                namespace = orchestrator.session.problem.namespace if hasattr(orchestrator, 'session') and orchestrator.session else "unknown"
                print(f"\n[DEBUG] Checking pod status in namespace '{namespace}'...")
                pod_list = orchestrator.kubectl.list_pods(namespace)
                print(f"Found {len(pod_list.items)} pods:")
                for pod in pod_list.items:
                    print(f"  - {pod.metadata.name}: {pod.status.phase}")
            except:
                pass
            raise
        
        # Incident 1회 실행
        result = await self.run_single_incident(orchestrator, "E_single", 1)
        results.append(result)
        
        # Cleanup
        try:
            orchestrator.session.problem.recover_fault()
            orchestrator.session.problem.app.cleanup()
        except:
            pass
        
        return results
    
    async def run_e_noreset(self, run_id: int) -> List[RunResult]:
        """E_noreset: reset 1회만 하고 incident 2회 연속 실행"""
        results = []
        
        # Agent 및 Orchestrator 초기화
        agent_registry = AgentRegistry()
        agent_cls = agent_registry.get_agent(self.config.agent_name)
        if agent_cls is None:
            raise ValueError(f"Unknown agent: {self.config.agent_name}")
        
        agent = agent_cls()
        orchestrator = Orchestrator()
        orchestrator.register_agent(agent, name=f"{self.config.agent_name}-agent")
        
        # Reset: 문제 초기화 (1회만)
        print(f"\n[E_noreset Run {run_id}] Resetting environment (once)...")
        try:
            problem_desc, instructs, apis = orchestrator.init_problem(self.config.problem_id)
            agent.init_context(problem_desc, instructs, apis)
        except Exception as e:
            print(f"[ERROR] Failed to initialize problem: {e}")
            # Pod 상태 확인
            try:
                namespace = orchestrator.session.problem.namespace if hasattr(orchestrator, 'session') and orchestrator.session else "unknown"
                print(f"\n[DEBUG] Checking pod status in namespace '{namespace}'...")
                pod_list = orchestrator.kubectl.list_pods(namespace)
                print(f"Found {len(pod_list.items)} pods:")
                for pod in pod_list.items:
                    print(f"  - {pod.metadata.name}: {pod.status.phase}")
            except:
                pass
            raise
        
        # Incident 1회 실행
        result1 = await self.run_single_incident(orchestrator, "E_noreset", 1)
        results.append(result1)
        
        # 중간 reset 없이 Incident 2회 실행
        # 새로운 문제 인스턴스 생성 (reset 없이)
        print(f"\n[E_noreset Run {run_id}] Running second incident (no reset)...")
        
        # Fault 재주입 (새로운 incident)
        try:
            orchestrator.session.problem.inject_fault()
        except:
            pass
        
        result2 = await self.run_single_incident(orchestrator, "E_noreset", 2)
        results.append(result2)
        
        # Cleanup
        try:
            orchestrator.session.problem.recover_fault()
            orchestrator.session.problem.app.cleanup()
        except:
            pass
        
        return results
    
    async def run_experiment(self, experiment_type: str = "both"):
        """
        전체 실험 실행
        
        Args:
            experiment_type: "both", "e_single", "e_noreset" 중 하나
        """
        print(f"\n{'='*60}")
        print(f"Starting Reset Comparison Experiment")
        print(f"Problem: {self.config.problem_id}")
        print(f"Agent: {self.config.agent_name}")
        print(f"Runs per condition: {self.config.num_runs}")
        print(f"Experiment type: {experiment_type}")
        print(f"{'='*60}\n")
        
        all_results = []
        
        # E_single 실행
        if experiment_type in ["both", "e_single"]:
            print(f"\n{'#'*60}")
            print(f"실험 1: E_single ({self.config.num_runs} runs)")
            print(f"{'#'*60}")
            for run_id in range(1, self.config.num_runs + 1):
                results = await self.run_e_single(run_id)
                all_results.extend(results)
                self.results.extend(results)
                time.sleep(10)  # Run 간 대기
        
        # E_noreset 실행
        if experiment_type in ["both", "e_noreset"]:
            print(f"\n{'#'*60}")
            print(f"실험 2: E_noreset ({self.config.num_runs} runs)")
            print(f"{'#'*60}")
            for run_id in range(1, self.config.num_runs + 1):
                results = await self.run_e_noreset(run_id)
                all_results.extend(results)
                self.results.extend(results)
                time.sleep(10)  # Run 간 대기
        
        # 결과 저장
        self.save_results(experiment_type)
        self.print_summary()
    
    def save_results(self, experiment_type: str = "both"):
        """결과 저장"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        experiment_suffix = experiment_type if experiment_type != "both" else "all"
        results_file = self.results_dir / f"reset_comparison_{experiment_suffix}_{timestamp}.json"
        
        results_data = {
            "config": asdict(self.config),
            "experiment_type": experiment_type,
            "results": [asdict(r) for r in self.results]
        }
        
        with open(results_file, "w") as f:
            json.dump(results_data, f, indent=2)
        
        print(f"\nResults saved to: {results_file}")
    
    def print_summary(self):
        """결과 요약 출력"""
        print(f"\n{'='*60}")
        print("Experiment Summary")
        print(f"{'='*60}\n")
        
        # 현재 설정 출력
        print(f"Current Thresholds:")
        print(f"  Latency (p95): < {self.config.latency_threshold} ms")
        print(f"  Error Rate: < {self.config.error_rate_threshold*100:.1f}%")
        print(f"  Success Window: {self.config.success_window}s")
        print(f"  Timeout: {self.config.timeout}s\n")
        
        # E_single 통계
        e_single_results = [r for r in self.results if r.condition == "E_single"]
        e_single_success = sum(1 for r in e_single_results if r.success)
        e_single_total = len(e_single_results)
        
        print(f"E_single:")
        if e_single_total > 0:
            print(f"  Success: {e_single_success}/{e_single_total} ({e_single_success/e_single_total*100:.1f}%)")
        else:
            print(f"  Success: {e_single_success}/{e_single_total} (N/A - no results)")
        if e_single_results:
            avg_recovery = sum(r.recovery_time for r in e_single_results if r.recovery_time) / len(e_single_results)
            print(f"  Avg Recovery Time: {avg_recovery:.2f}s")
            
            # 실패한 실행들의 상세 정보
            failed_results = [r for r in e_single_results if not r.success]
            if failed_results:
                print(f"\n  Failed Runs Details ({len(failed_results)}/{e_single_total}):")
                for i, r in enumerate(failed_results, 1):
                    print(f"    Run {i}:")
                    if r.final_latency_p95 is not None:
                        latency_status = "✓" if r.final_latency_p95 < self.config.latency_threshold else "✗"
                        print(f"      Latency (p95): {r.final_latency_p95:.2f} ms {latency_status} (threshold: {self.config.latency_threshold} ms)")
                    if r.final_error_rate is not None:
                        error_status = "✓" if r.final_error_rate < self.config.error_rate_threshold else "✗"
                        print(f"      Error Rate: {r.final_error_rate*100:.2f}% {error_status} (threshold: {self.config.error_rate_threshold*100:.1f}%)")
                    if r.failure_reason:
                        print(f"      Reason: {r.failure_reason}")
            
            # 성공한 실행들의 평균 메트릭
            success_results = [r for r in e_single_results if r.success]
            if success_results:
                print(f"\n  Successful Runs Metrics ({len(success_results)}/{e_single_total}):")
                avg_latency = sum(r.final_latency_p95 for r in success_results if r.final_latency_p95) / len([r for r in success_results if r.final_latency_p95])
                avg_error = sum(r.final_error_rate for r in success_results if r.final_error_rate is not None) / len([r for r in success_results if r.final_error_rate is not None])
                print(f"    Avg Latency (p95): {avg_latency:.2f} ms")
                print(f"    Avg Error Rate: {avg_error*100:.2f}%")
        
        # E_noreset 통계
        e_noreset_results = [r for r in self.results if r.condition == "E_noreset"]
        e_noreset_attempt1 = [r for r in e_noreset_results if r.incident_attempt == 1]
        e_noreset_attempt2 = [r for r in e_noreset_results if r.incident_attempt == 2]
        
        attempt1_success = sum(1 for r in e_noreset_attempt1 if r.success)
        attempt2_success = sum(1 for r in e_noreset_attempt2 if r.success)
        
        print(f"\nE_noreset:")
        if len(e_noreset_attempt1) > 0:
            print(f"  Attempt 1 - Success: {attempt1_success}/{len(e_noreset_attempt1)} ({attempt1_success/len(e_noreset_attempt1)*100:.1f}%)")
        else:
            print(f"  Attempt 1 - Success: {attempt1_success}/{len(e_noreset_attempt1)} (N/A - no results)")
        if len(e_noreset_attempt2) > 0:
            print(f"  Attempt 2 - Success: {attempt2_success}/{len(e_noreset_attempt2)} ({attempt2_success/len(e_noreset_attempt2)*100:.1f}%)")
        else:
            print(f"  Attempt 2 - Success: {attempt2_success}/{len(e_noreset_attempt2)} (N/A - no results)")
        
        if e_noreset_attempt1:
            avg_recovery1 = sum(r.recovery_time for r in e_noreset_attempt1 if r.recovery_time) / len(e_noreset_attempt1)
            print(f"  Attempt 1 - Avg Recovery Time: {avg_recovery1:.2f}s")
        if e_noreset_attempt2:
            avg_recovery2 = sum(r.recovery_time for r in e_noreset_attempt2 if r.recovery_time) / len(e_noreset_attempt2)
            print(f"  Attempt 2 - Avg Recovery Time: {avg_recovery2:.2f}s")
        
        print(f"\n{'='*60}")


async def main():
    """메인 함수"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Reset Comparison Experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
실험 타입 선택:
  both      - 두 실험 모두 실행 (기본값)
  e_single  - 실험 1만 실행 (각 run마다 reset 후 incident 1회)
  e_noreset - 실험 2만 실행 (reset 1회 후 incident 2회 연속)

예시:
  # 실험 1만 실행
  python3 reset_comparison_experiment.py --problem-id pod_kill_hotel_res-detection-1 --experiment e_single
  
  # 실험 2만 실행
  python3 reset_comparison_experiment.py --problem-id pod_kill_hotel_res-detection-1 --experiment e_noreset
  
  # 두 실험 모두 실행
  python3 reset_comparison_experiment.py --problem-id pod_kill_hotel_res-detection-1 --experiment both
        """
    )
    parser.add_argument("--problem-id", type=str, required=True,
                       help="Problem ID (e.g., pod_kill_hotel_res-detection-1)")
    parser.add_argument("--agent", type=str, default="gpt",
                       help="Agent name (default: gpt)")
    parser.add_argument("--experiment", type=str, default="both",
                       choices=["both", "e_single", "e_noreset"],
                       help="실험 타입 선택: both, e_single, e_noreset (default: both)")
    parser.add_argument("--runs", type=int, default=5,
                       help="Number of runs per condition (default: 5)")
    parser.add_argument("--latency-threshold", type=float, default=1000.0,
                       help="p95 latency threshold in ms (default: 1000.0)")
    parser.add_argument("--error-rate-threshold", type=float, default=0.01,
                       help="Error rate threshold (default: 0.01)")
    parser.add_argument("--success-window", type=int, default=60,
                       help="Success window in seconds (default: 60)")
    parser.add_argument("--timeout", type=int, default=600,
                       help="Timeout in seconds (default: 600)")
    parser.add_argument("--max-steps", type=int, default=20,
                       help="Max steps for agent (default: 20)")
    
    args = parser.parse_args()
    
    config = ExperimentConfig(
        problem_id=args.problem_id,
        agent_name=args.agent,
        latency_threshold=args.latency_threshold,
        error_rate_threshold=args.error_rate_threshold,
        success_window=args.success_window,
        timeout=args.timeout,
        num_runs=args.runs,
        max_steps=args.max_steps
    )
    
    experiment = ResetComparisonExperiment(config)
    await experiment.run_experiment(args.experiment)


if __name__ == "__main__":
    asyncio.run(main())
