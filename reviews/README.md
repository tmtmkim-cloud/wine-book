# 와인 리뷰 지도 (WineBook 부록)

와인 블로그의 시음 리뷰를 모아 점수·등급, 나라·지역·품종별로 보여 주는 페이지입니다.

- 페이지: https://tmtmkim-cloud.github.io/wine-book/reviews/
- 미리보기(가상 데이터): https://tmtmkim-cloud.github.io/wine-book/reviews/?demo

## 동작 방식

```
블로그 RSS ─┐
            ├─▶ GitHub Actions 수집기 ─▶ Claude API로 정리 ─▶ data/wines.json ─▶ 리뷰 페이지
backfill.txt┘   (매일 06:37 + 새로고침 버튼)   (웹 검색으로 양조 정보 보강)
```

1. `data/sources.json`에 적힌 블로그의 RSS에서 새 글을 찾습니다.
2. `data/backfill.txt`에 적힌 과거 글 주소를 대기열에 넣습니다.
3. 글 본문을 Claude API(기본 `claude-sonnet-5-5`)에 보내 와인마다 이름, 생산자, 빈티지, 종류, 나라·지역·아펠라시옹, 분류 등급, 품종, 블로거 점수·등급, 평가 요약, 양조 방법, 좌표를 정리합니다. 양조 방법은 글에 없으면 웹 검색으로 생산자 자료를 찾아 보강합니다.
4. 결과를 `data/*.json`에 커밋하면 페이지에 반영됩니다.

수집기는 `.github/workflows/wine-reviews.yml`, 코드는 `collector/collect.py`(파이썬 표준 라이브러리만 사용)입니다.

## 처음 설정 (한 번만)

1. **Claude API 키** — [Claude Console](https://platform.claude.com/settings/keys)에서 키를 만들고, 이 저장소의 Settings → Secrets and variables → Actions → New repository secret에 이름 `ANTHROPIC_API_KEY`로 저장합니다.
2. **웹 검색 켜기(권장)** — Console의 [Privacy 설정](https://platform.claude.com/settings/privacy)에서 웹 검색을 허용합니다. 꺼져 있으면 양조 정보를 글과 모델 지식으로만 정리하고, 수집 상태에 그렇게 표시합니다.
3. **새로고침 버튼 연결(선택)** — GitHub Settings → Developer settings → Fine-grained tokens에서 토큰을 만듭니다.
   - Repository access: Only select repositories → `wine-book`
   - Permissions: **Actions**, **Contents**를 Read and write
   - 리뷰 페이지 → 설정 → GitHub 연결에 붙여 넣습니다. 토큰은 그 기기의 브라우저에만 저장됩니다.
4. **첫 수집** — 페이지의 새로고침 버튼을 누르거나, Actions → 와인 리뷰 수집 → Run workflow를 누릅니다.
   - 먼저 `mode: check`(페이지 설정 → 도구 → 접속 점검)로 돌려 보면 API 비용 없이 RSS와 본문을 읽을 수 있는지 확인할 수 있습니다.

## 블로그 추가, 과거 글 추가

- **블로그 추가**: 페이지 설정 → 블로그에 주소를 넣고 추가합니다. `data/sources.json`을 직접 고쳐도 됩니다.
  - 네이버: `https://rss.blog.naver.com/<아이디>.xml` · 티스토리: `https://<이름>.tistory.com/rss` · 그 밖: RSS 또는 Atom 주소
  - `hint`에 블로거의 점수·등급 체계를 적어 두면 정리가 더 정확해집니다. 예: `"100점 만점. 글 끝의 '추천도: A/B/C'가 등급"`
  - `enabled: false`면 RSS 수집을 쉬고, 이미 정리한 와인은 그대로 둡니다.
- **과거 글**: RSS에는 최근 글만 나옵니다. 페이지 설정 → 과거 글 추가에 글 주소를 붙여 넣으면 `data/backfill.txt`에 저장하고 바로 수집을 실행합니다. 한 번 실행에 `max_posts_per_run`개씩 처리하고 남은 글은 다음 실행에서 이어서 처리합니다.

## 설정값 (`data/sources.json` → `settings`)

| 키 | 기본값 | 뜻 |
|---|---|---|
| `model` | `claude-sonnet-5-5` | 정리에 쓸 모델. 비용을 줄이려면 `claude-haiku-5-5` |
| `max_posts_per_run` | 20 | 한 번 실행에서 정리할 최대 글 수 |
| `web_search` | true | 양조 정보 보강에 웹 검색 사용 |
| `web_search_max_uses` | 3 | 글 하나당 최대 검색 횟수 |

## 비용 (참고)

- Claude Sonnet 5.5는 입력 100만 토큰당 $2, 출력 $10이고 웹 검색은 1,000회당 $10입니다. 글 길이와 검색 횟수에 따라 글 하나에 대략 $0.03~0.15가 듭니다.
- Haiku 5.5는 입력 $0.10, 출력 $0.50(100만 토큰당)이라 훨씬 쌉니다.
- 실행마다 실제 토큰·검색 수와 추정 비용을 페이지 설정 → 수집 상태에 보여 줍니다.
- GitHub Actions는 공개 저장소라 무료입니다.

## 파일

| 파일 | 내용 |
|---|---|
| `data/wines.json` | 정리된 와인 목록 |
| `data/state.json` | 글별 처리 기록(대기·완료·건너뜀·실패) |
| `data/status.json` | 최근 실행 결과와 기록 |
| `data/sources.json` | 블로그 목록과 설정 |
| `data/backfill.txt` | 과거 글 주소 |
| `data/sample.json` | 미리보기용 가상 데이터(`?demo`) |

## 다시 정리, 문제 해결

- **다시 정리**: Run workflow에서 `mode: reprocess`(페이지 설정 → 도구 → 다시 정리). 정리한 지 오래된 글부터 한 번에 `max_posts_per_run`개씩 다시 정리합니다.
- 실패한 글은 다음 실행에서 세 번까지 다시 시도합니다.
- 블로그 서버가 본문 접속을 막으면 RSS 요약으로 정리하고 상세에 "요약만"으로 표시합니다.
- 최근 실행 결과는 페이지 상단의 "마지막 수집" 줄과 설정 → 수집 상태에서 볼 수 있습니다.
- GitHub는 활동이 60일 동안 없는 공개 저장소의 예약 실행을 멈춥니다. 매일 실행 결과를 커밋하므로 보통은 문제가 없고, 멈추면 Actions 화면에서 다시 켜면 됩니다.

## 저작권

리뷰 원문, 점수, 사진의 권리는 각 블로거에게 있습니다. 이 저장소는 공개 저장소이므로 블로그 본문을 저장하지 않고, Claude가 다시 쓴 짧은 요약과 원문 링크, 와인에 대한 사실 정보만 저장합니다.
