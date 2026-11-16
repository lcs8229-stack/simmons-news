# 시몬스 뉴스

공개 뉴스 큐레이션 사이트입니다.
정치·경제·사회·문화·국제 주요 뉴스를 **국내 60% / 해외 40%** 비중으로 고르고,
과학 저널과 전기·에너지 전문지 소식을 별도 탭으로 보여줍니다.

- API 키 없이 동작합니다. 중요도는 "같은 사건을 몇 개 언론사가 보도했는지"와 최신성으로 매깁니다.
- 기사 본문은 저장하지 않고 제목·요약·원문 링크만 보여줍니다.
- GitHub Actions 가 매시간 수집해 GitHub Pages 로 배포합니다 (무료).

[ai-news-radar](https://github.com/Bronc-X/ai-news-radar-for-rss-news) 의 수집 도구 일부
(시간 파싱, URL 정규화, ID 생성, 재시도 세션)를 `scripts/radar_lib.py` 로 가져와 썼습니다.

## 구조

```
feeds/sources.opml      소스 목록 (언론사·국내/해외·분야·성향·AI 처리 허용 여부)
scripts/radar_lib.py    원 프로젝트에서 가져온 공통 함수
scripts/outlets.py      언론사 정규화 · AI 활용 정책 판정 (feeds/outlets.json)
scripts/source_health.py 소스 상태 판정(요청·파싱·유효·최근)과 실행 간 이력
scripts/quality_gate.py 배포 전 품질 검사 (기준: config/quality.json, 7일 중앙값 비교)
scripts/state_snapshot.py 상태 스냅샷 만들기·검증·복구 (캐시 유실 대비, Actions artifact 30일)
docs/OPERATIONS.md      운영 안내: 상태 정의, 배포 보류 기준, 캐시 유실·예약 중단 복구, 외부 감시
docs/SELECTION.md       사건 묶기·관련 이슈·사건 ID·대상 지역·대표 기사·주요 뉴스 선정 규칙
scripts/theme.py        테마 탭: 주제 관련도 판단·28일 후보 보관·선정 (config/themes.json)
docs/THEMES.md          테마 탭 규칙, 5단계 AI 검토 항목, 한계
tools/compare_selection.py 같은 기사 표본에 이전·현재 선정 방식을 적용해 비교
scripts/build_news.py   수집 → 아카이브 → 사건 묶기 → 점수 → 60:40 선정 → data/news.json
index.html, assets/     화면 (주요 / 국내 / 해외 / 과학·전기 / 테마 탭, 분야 필터·주제 선택, 검색)
.github/workflows/      매시간 수집·배포
```

처리 흐름:

1. **수집**: `sources.opml` 의 RSS 를 가져옵니다. 같은 사이트는 순서대로 요청합니다(한국경제는 동시 요청 시 403).
2. **아카이브**: 언론사+URL 로 중복을 없애고 일반 뉴스는 3일, 과학은 31일 보관합니다. 저장소가 아니라 Actions 캐시에 둡니다.
3. **사건 묶기**: 제목을 한글 2글자 단위·영단어로 나눠 IDF 가중 유사도로 비교합니다. 각 사건의 첫 기사와 직접 비교하고, 최초 기사부터 24시간 안에서만 묶으며, 다른 기업·인물·날짜·반대 표현이면 합치지 않습니다. 관련 사건은 이슈로 연결합니다.
4. **분류**: 피드가 정한 분야를 우선 쓰고, 종합 피드는 키워드로 분류합니다. 인사·부고·방송 편성표는 뺍니다.
5. **점수·선정**: `(1 + 2.2·log2(보도 계열 수)) × 최신성` (거의 같은 제목의 전재는 1곳으로 셈). 사건 대상 기준 국내 24 / 해외 16건을 고르고, 분야는 할당의 40% 내림까지, 한 이슈는 2칸까지입니다.

## 기사 데이터 규칙 (1단계)

**AI 활용 정책** — `feeds/outlets.json`, `scripts/outlets.py`

- 기사마다 수집 경로(`via`: `direct` / `google_news`)와 원언론사(`outlet`)를 따로 기록합니다.
  Google 뉴스 경유 기사는 `<source>` 의 이름·도메인으로 원언론사를 찾고, 별칭(예: Yonhap News Agency → 연합뉴스)을 정규화합니다.
- 판정: 피드 `ai="no"` · 원언론사 `deny` · 경로 `deny` 중 하나라도 있으면 금지.
  원언론사와 경로가 **모두 `allow`** 일 때만 허용. 정책 표에 없거나 `unknown` 이면 허용하지 않습니다.
- 현재 근거 있는 금지: 연합뉴스, 한겨레 (RSS 에 "AI 학습 및 활용 금지"). 나머지는 모두 미확인이라 AI 처리 대상이 0건입니다.
- 매 실행마다 아카이브 전체를 현재 정책 표로 재판정합니다. AI 요청을 만드는 코드는 반드시
  `outlets.ai_gate()` 를 거쳐야 하며, 저장된 `ai_ok` 값은 믿지 않고 다시 판정합니다.

**시간** — 기사마다 다음을 구분합니다.

| 필드 | 뜻 |
|---|---|
| `published_at` | 발행 시각 (RSS `pubDate`, Atom `published`, RSS 1.0 `dc:date`) |
| `updated_at` | 수정 시각 (Atom `updated`). 피드에 실제로 있을 때만 |
| `time_basis` | 근거: `published` / `dc:date` / `updated_only` / `unknown` / `invalid_future` / `legacy`(이전 형식) |
| `first_seen_at` / `last_seen_at` | 최초·마지막 수집 시각. **보도 시각으로 쓰지 않음** |

- 시간대 없는 값은 소스 기준(국내 `Asia/Seoul`, 해외 `UTC`, OPML `tz` 속성으로 변경)으로 해석합니다.
- 시각 미상 기사는 사건을 새로 만들거나 대표가 될 수 없고, 같은 사건이 있으면 "함께 보도한 언론사"로만 붙습니다.
  붙지 않은 것은 `news.json` 의 `undated` 목록에 따로 둡니다.
- 사건(story)의 `published_at`·제목·링크·언론사는 모두 **대표 기사 하나**에서 옵니다.
  사건 전체의 최신 보도 시각은 `latest_reported_at` 입니다.

**같은 URL 갱신** — 제목·요약이 바뀌면 새 값으로 바꾸고 `revision`, `content_changed_at`, `previous_title` 을 남깁니다.
`first_seen_at` 은 유지합니다. 아카이브는 `schema: 2` 이며, 이전 형식은 읽을 때 자동 변환합니다.

## 수집·배포 안정성 (2단계)

자세한 내용은 [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

- 소스마다 요청 → 파싱 → 유효 기사 → 시각 확인 → 최근 기사를 구분해 `ok / undated / stale / filtered_all / empty / parse_error / http_error` 로 판정합니다. 0건은 정상으로 표시하지 않습니다.
- 배포 전 품질 검사(`scripts/quality_gate.py`)가 실패하면 배포하지 않고 이전 정상 페이지를 유지합니다. 일부 소스 실패는 경고만 남깁니다.
- 화면은 데이터 생성 시각이 3시간을 넘으면 스스로 "갱신 지연" 을 표시합니다. 알림은 별도 설정(Healthchecks.io)이 필요합니다.

## 사건 묶기·주요 뉴스 선정 (3단계)

자세한 규칙은 [`docs/SELECTION.md`](docs/SELECTION.md).

- 같은 사건은 최초 기사부터 24시간 안, 다른 기업·인물·날짜·반대 표현이면 합치지 않습니다. 사건 ID 는 대표 기사와 분리되어 실행 간에 유지됩니다.
- 관련 사건은 '이슈'로 연결하고, 주요 뉴스에서 한 이슈는 최대 2칸입니다.
- 국내 60 / 해외 40 은 사건 대상 지역 기준입니다(`target_region`). 분야 상한은 할당의 40% 내림이며 넘겨 채우지 않습니다.
- 비교: `python tools/compare_selection.py --archive data/archive.json --old <이전 build_news.py>`

## 테마 탭 (4단계)

자세한 내용은 [`docs/THEMES.md`](docs/THEMES.md). 주제 목록과 기준은 [`config/themes.json`](config/themes.json) 한곳에서 고칩니다.

- 선택된 주제와 기사 관련도를 기준으로 주제 기사 6건 + 시야 넓히기 4건(기본값)을 고르고, 최근 28일 후보를 따로 보관합니다. 기준 미달이면 채우지 않습니다.
- 관련도·글의 성격·출처·최신성·감점·중복을 따로 판단하는 규칙 기반이며 API 키가 필요 없습니다.
- 카드는 원문 제목·원출처 링크·매체·발행일·태그만 보여주고 번역·요약은 넣지 않습니다.

## 로컬 실행

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
python scripts/build_news.py
python scripts/quality_gate.py   # 종료 코드 1 이면 배포 보류 대상
python -m http.server 8080   # http://localhost:8080
```

## 배포 (GitHub Pages, 무료)

1. 이 저장소를 GitHub 에 올립니다.
2. Settings → Pages → Source 를 **GitHub Actions** 로 바꿉니다.
3. Actions 탭에서 `Update and deploy` 를 한 번 수동 실행합니다. 이후 매시간 자동 실행됩니다.

무료 Pages 는 공개 페이지입니다. 주소를 아는 사람은 누구나 볼 수 있습니다.

## 소스 추가

`feeds/sources.opml` 에 `outline` 한 줄을 추가합니다. 속성 설명은 파일 맨 위 주석에 있습니다.
공식 RSS 가 없는 곳은 Google 뉴스 검색 RSS(`gnews="1"`)로 넣을 수 있지만, 비공식이라 언제든 막힐 수 있습니다.

## 다음 단계: Claude 편집자 (선택)

`news.json` 의 각 사건에는 `title_ko`, `summary_ko` 가 비어 있습니다. Claude 편집 단계가 채우면 화면이 자동으로 그 값을 씁니다.
계획: 가장 저렴한 모델로 해외 기사 번역·요약, 정밀 분류, 사건 묶기 보정, 하루 2회 브리핑.
`ai="no"` 로 표시된 소스(연합뉴스: 약관에 "AI 학습 및 활용 금지")는 Claude 로 보내지 않습니다.
