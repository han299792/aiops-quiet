# 캠페인 중에는 배포하지 않는 수정들

**`post-campaign` 브랜치에 있다. observe·block 두 팔이 다 끝나기 전에는
`main`에 병합하지도, 파드에서 pull 하지도 않는다.**

## 왜 미루나

캠페인은 지금 커밋 **`061b9ab`** 로 돌고 있다. 실행 중에 코드를 바꾸면
회차마다 다른 코드가 돌아간 결과가 섞이고, `manifest.json`의 `quiet_sha`가
회차별로 갈린다. **사전등록의 재현성 계약이 정확히 그걸 막으라고 있다.**

아래 둘 다 **측정값을 바꾸지 않는다.** 하나는 메타데이터, 하나는 기록 형식이다.
그래서 미루는 비용이 0이고, 지금 넣는 비용은 실험 전체의 출처가 흐려지는 것이다.

## 미뤄둔 것 두 개

### 1. `quiet_dirty`가 매 회차 거짓 경보를 낸다

`manifest.py`가 `git status --porcelain`으로 판정하는데 이건 **추적 안 되는
파일도 센다.** 캠페인이 결과를 레포 안 `runs/`에 쓰므로 **첫 회차 이후 전부**
`quiet_dirty: true`가 된다 — 코드는 깨끗한데도.

실측 (11:22, 파드):
```
$ git status --porcelain
?? runs/2026-09-12T11-10-19Z__.../
?? runs/_pilot/
?? runs/ledger.json          ← 전부 결과물. 추적 코드 수정은 0건
$ git log --oneline -1
061b9ab fix: persist blocked_actions and count agent turns
```

**매번 울리는 경보는 경보가 아니다.** 진짜로 코드가 커밋 안 된 채 돌아간
회차가 생겨도 아무도 안 본다. `preflight.py`도 같은 문구를 낸다.

→ 추적 파일 수정만 `quiet_dirty`로, 추적 안 된 파일 수는 `quiet_untracked`로 분리.

**이번 캠페인 해석:** 모든 회차의 `quiet_dirty: true`는 **무시해도 된다.**
근거는 위 실측이고, 실제 코드 커밋은 `061b9ab` 하나다.

### 2. 트레이스에 관측이 두 번 기록된다

`_drive_agent`가 `orch.ask_env(action)`을 부르는데, **`ask_env`가 이미
`session.add({"role": "env"})`를 한다** (`orchestrator.py:141`). 그 뒤에
루프가 한 번 더 더한다.

파일럿 트레이스: **assistant 4개, env 8개.** `leak.json`의 히트도 두 벌씩
찍힌다 (step 7과 step 8이 동일 excerpt).

차단된 액션은 `ask_env`를 안 거치므로 **한 번만** 기록된다 →
**두 팔의 트레이스 모양이 다르다.** 모듈 docstring의 "세션 JSON이 업스트림과
바이트 호환"이라는 주장도 이것 때문에 거짓이다.

**측정에는 영향 없다.** `leaked`·`turns`·`first_leak_step` 전부 그대로다
(`turns`는 assistant만 세고, `first_leak_step`은 둘 중 앞선 것을 잡는다).
바뀌는 건 트레이스 크기와 히트 중복뿐이다.

→ 차단 분기에서만 `session.add`, 정상 분기는 `ask_env`에 맡긴다.

## 캠페인 끝나고 할 일

```bash
git checkout main && git merge post-campaign
# 파드에서
cd /work/aiops-quiet && git pull
```

그리고 **재코딩하지 않는다.** 둘 다 측정값을 안 바꾸므로 §7.5 대상이 아니다.
`docs/3-PREREG.md` §9에 "배포 시점"만 한 줄 적는다.
