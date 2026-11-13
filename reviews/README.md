# 와인 리뷰 지도 (WineBook 부록)

와인 블로그의 시음 리뷰를 모아 점수·등급, 나라·지역·품종별로 보여 주는 페이지입니다.

- 페이지: https://tmtmkim-cloud.github.io/wine-book/reviews/
- 미리보기(가상 데이터): https://tmtmkim-cloud.github.io/wine-book/reviews/?demo

## 동작 방식

자동 예약 실행은 없습니다. 필요할 때 한 번 실행하면 블로그의 지금까지 글을 정리합니다.

```
블로그 전체 글 목록 ─▶ 제목으로 와인 글 고르기 ─▶ 글마다 Claude API로 정리 ─▶ data/wines.json ─▶ 리뷰 페이지
(네이버 글 목록·사이트맵)   (Haiku, 애매하면 포함)      (웹 검색으로 양조 정보 보강)
```

1. `data/sources.json`에 적힌 블로그의 전체 글 목록을 가져옵니다. 네이버 블로그는 블로그 글 목록, 티스토리·워드프레스 등은 사이트맵을 쓰고, 둘 다 안 되면 RSS의 최근 글만 가져옵니다.
2. 제목으로 와인 글을 고릅니다. 저렴한 모델(`claude-haiku-5-5`)로 한꺼번에 분류하고, 애매하면 포함합니다.
3. 고른 글의 본문을 Claude API(기본 `claude-sonnet-5-5`)로 정리합니다. 와인마다 이름, 생산자, 빈티지, 종류, 나라·지역·아펠라시옹, 분류 등급, 품종, 블로거 점수·등급, 평가 요약, 양조 방법, 좌표를 뽑고, 양조 방법이 글에 없으면 웹 검색으로 생산자 자료를 찾아 보강합니다.
4. 결과를 `data/*.json`에 커밋하면 페이지에 반영됩니다.

이미 정리한 글은 건너뛰므로 다시 실행하면 그사이 새로 생긴 글만 정리합니다. 글이 많으면 한 번 실행에서 정해진 개수만 정리하고, 남은 글은 워크플로가 다음 실행을 스스로 이어 붙여 끝까지 정리합니다.

수집기는 `.github/workflows/wine-reviews.yml`(이름: 와인 리뷰 정리), 코드는 `collector/collect.py`(파이썬 표준 라이브러리만 사용)입니다.

## 처음 한 번 정리하기

1. **Claude API 키** — [Claude Console](https://platform.claude.com/settings/keys)에서 키를 만들고, 이 저장소의 Settings → Secrets and variables → Actions → New repository secret에 이름 `ANTHROPIC_API_KEY`로 저장합니다. 크레딧이 없으면 Console의 Billing에서 충전합니다.
2. **웹 검색 켜기(권장)** — Console의 [Privacy 설정](https://platform.claude.com/settings/privacy)에서 웹 검색을 허용합니다. 꺼져 있으면 양조 정보를 글과 모델 지식으로만 정리하고, 정리 상태에 그렇게 표시합니다.
3. **접속 점검(비용 없음)** — Actions → 와인 리뷰 정리 → Run workflow에서 mode `check`로 실행합니다. 블로그별 글 수와 기간, 본문을 읽을 수 있는지가 페이지 상단과 설정 → 정리 상태에 나옵니다.
4. **전체 정리** — 같은 곳에서 mode `all`로 실행합니다. 글이 많으면 몇 시간 동안 자동으로 이어서 진행하고, 진행 상황이 페이지에 표시됩니다.

GitHub 토큰을 리뷰 페이지 설정에 넣으면 3·4번을 페이지에서 바로 실행할 수 있습니다(설정 → 블로그 정리).
- 토큰 만들기: GitHub Settings → Developer settings → Fine-grained tokens
- Repository access: Only select repositories → `wine-book`
- Permissions: **Actions**, **Contents**를 Read and write
- 토큰은 그 기기의 브라우저에만 저장됩니다.

## 나중에 다른 블로그 추가하기

1. 페이지 설정 → 블로그 추가에 블로그 주소를 넣습니다(네이버, 티스토리, RSS 주소). `data/sources.json`에 직접 적어도 됩니다.
2. 목록에서 그 블로그의 **정리**를 누르거나, Run workflow에서 mode `all`, source에 블로그 id를 넣어 실행합니다.

- `hint`에 블로거의 점수·등급 체계를 적어 두면 정리가 더 정확해집니다. 예: `"100점 만점. 글 끝의 '추천도: A/B/C'가 등급"`
- `enabled: false`인 블로그는 전체 정리에서 빠집니다. 이미 정리한 와인은 그대로 남습니다.
- 전체 목록을 읽지 못하는 블로그나 특정 글만 정리하려면 설정 → 글 주소로 추가(또는 `data/backfill.txt`)에 글 주소를 넣습니다.

## Run workflow 입력값

| 입력 | 뜻 |
|---|---|
| `mode` | `all` 전체 정리, `check` 접속 점검(API 비용 없음), `reprocess` 정리한 글 다시 정리 |
| `source` | 이 블로그 id만 처리. 비우면 켜 둔 블로그 전체 |
| `title_filter` | `off`면 제목 필터 없이 모든 글을 정리하고, 전에 제목으로 거른 글도 본문으로 다시 확인 |
| `limit` | 한 번 실행에서 정리할 최대 글 수 |
| `chain` | 자동 이어서 실행 횟수. 직접 실행할 때는 0 그대로 |

## 설정값 (`data/sources.json` → `settings`)

| 키 | 기본값 | 뜻 |
|---|---|---|
| `model` | `claude-sonnet-5-5` | 글 정리에 쓸 모델. 비용을 줄이려면 `claude-haiku-5-5` |
| `filter_model` | `claude-haiku-5-5` | 제목으로 와인 글을 고를 때 쓸 모델 |
| `title_filter` | true | 제목 필터 사용 |
| `max_posts_per_run` | 40 | 한 번 실행에서 정리할 최대 글 수(남은 글은 자동으로 이어서) |
| `web_search` | true | 양조 정보 보강에 웹 검색 사용 |
| `web_search_max_uses` | 3 | 글 하나당 최대 검색 횟수 |

## 비용 (참고)

- 글 정리: Claude Sonnet 5.5는 입력 100만 토큰당 $2, 출력 $10이고 웹 검색은 1,000회당 $10입니다. 와인 글 하나에 대략 $0.03~0.15가 듭니다.
- 제목 분류: Haiku 5.5로 150개씩 묶어 분류하므로 전체 비용에서 아주 작습니다.
- 예: 와인 글이 300개라면 대략 $10~45입니다. 접속 점검으로 블로그별 글 수를 먼저 확인하세요.
- `model`을 `claude-haiku-5-5`로 바꾸면 정리 비용이 크게 줄어듭니다(입력 $0.10, 출력 $0.50).
- 실행마다 실제 토큰·검색 수와 추정 비용을 페이지 설정 → 정리 상태에 보여 줍니다.
- GitHub Actions는 공개 저장소라 무료입니다.

## 파일

| 파일 | 내용 |
|---|---|
| `data/wines.json` | 정리된 와인 목록 |
| `data/state.json` | 글별 처리 기록(목록·대기·완료·건너뜀·실패)과 블로그별 목록 정보 |
| `data/status.json` | 최근 실행 결과와 기록 |
| `data/sources.json` | 블로그 목록과 설정 |
| `data/backfill.txt` | 직접 추가한 글 주소 |
| `data/sample.json` | 미리보기용 가상 데이터(`?demo`) |

## 문제 해결

- 실패한 글은 다음 실행에서 세 번까지 다시 시도합니다.
- 블로그 서버가 본문 접속을 막으면 RSS 요약으로 정리하고 상세에 "요약만"으로 표시합니다.
- 제목 필터가 와인 글을 놓친 것 같으면 `title_filter: off`로 한 번 실행하세요. 제목으로 거른 글을 본문으로 다시 확인합니다.
- 최근 실행 결과는 페이지 상단의 "마지막 정리" 줄과 설정 → 정리 상태에서 볼 수 있습니다.

## 저작권

리뷰 원문, 점수, 사진의 권리는 각 블로거에게 있습니다. 이 저장소는 공개 저장소이므로 블로그 본문을 저장하지 않고, Claude가 다시 쓴 짧은 요약과 원문 링크, 와인에 대한 사실 정보만 저장합니다.
