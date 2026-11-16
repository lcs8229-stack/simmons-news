# 시몬스 뉴스

네이버 뉴스 앱 대신 매일 보는 개인 뉴스 페이지입니다.
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
scripts/build_news.py   수집 → 아카이브 → 사건 묶기 → 점수 → 60:40 선정 → data/news.json
index.html, assets/     화면 (주요 / 국내 / 해외 / 과학·전기 탭, 분야 필터, 검색)
.github/workflows/      매시간 수집·배포
```

처리 흐름:

1. **수집**: `sources.opml` 의 RSS 를 가져옵니다. 같은 사이트는 순서대로 요청합니다(한국경제는 동시 요청 시 403).
2. **아카이브**: 언론사+URL 로 중복을 없애고 일반 뉴스는 3일, 과학은 31일 보관합니다. 저장소가 아니라 Actions 캐시에 둡니다.
3. **사건 묶기**: 제목을 한글 2글자 단위·영단어로 나눠 IDF 가중 유사도로 비교합니다. 각 사건의 첫 기사와 직접 비교해 서로 다른 사건이 줄줄이 엮이는 것을 막습니다.
4. **분류**: 피드가 정한 분야를 우선 쓰고, 종합 피드는 키워드로 분류합니다. 인사·부고·방송 편성표는 뺍니다.
5. **점수·선정**: `(1 + 2.2·log2(언론사 수)) × 최신성`. 국내 24 / 해외 16건을 고르고, 국내는 한 분야가 40%를 넘지 않게 합니다.

## 로컬 실행

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
python scripts/build_news.py
python -m http.server 8080   # http://localhost:8080
```

## 배포 (GitHub Pages, 무료)

1. 이 저장소를 GitHub 에 올립니다.
2. Settings → Pages → Source 를 **GitHub Actions** 로 바꿉니다.
3. Actions 탭에서 `Update and deploy` 를 한 번 수동 실행합니다. 이후 매시간 자동 실행됩니다.

무료 Pages 는 공개 페이지입니다. 주소를 아는 사람은 누구나 볼 수 있습니다 (개인 정보는 없음).

## 소스 추가

`feeds/sources.opml` 에 `outline` 한 줄을 추가합니다. 속성 설명은 파일 맨 위 주석에 있습니다.
공식 RSS 가 없는 곳은 Google 뉴스 검색 RSS(`gnews="1"`)로 넣을 수 있지만, 비공식이라 언제든 막힐 수 있습니다.

## 다음 단계: Claude 편집자 (선택)

`news.json` 의 각 사건에는 `title_ko`, `summary_ko` 가 비어 있습니다. Claude 편집 단계가 채우면 화면이 자동으로 그 값을 씁니다.
계획: 가장 저렴한 모델로 해외 기사 번역·요약, 정밀 분류, 사건 묶기 보정, 하루 2회 브리핑.
`ai="no"` 로 표시된 소스(연합뉴스: 약관에 "AI 학습 및 활용 금지")는 Claude 로 보내지 않습니다.
