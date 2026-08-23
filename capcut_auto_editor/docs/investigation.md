# 착수 전 조사 기록 (요청서 2절)

조사일 기준 환경: Python 3.11.15 / `pycapcut==0.0.3` 실제 설치본
(`pip download` 후 소스 직접 열람 + 실제 드래프트 생성 후 JSON 검증).

> **이 문서의 규칙**: 아래는 전부 *실행해서 확인한 것*입니다.
> 요청서 3절과 어긋나는 항목은 **"⚠ 요청서와 다름"** 으로 표시했고,
> 요청서 지침대로 **3절이 아니라 실물을 따랐습니다.**

---

## 1. pycapcut 0.0.3 실제 시그니처

`inspect.signature`로 뽑은 실측값입니다. (요청서에 없던 부분이라 전부 새로 확인)

```python
DraftFolder(folder_path: str)
  .create_draft(draft_name, width, height, fps=30, *, allow_replace=False) -> ScriptFile
  .duplicate_as_template(template_name, new_draft_name, allow_replace=False) -> ScriptFile
  .load_template(draft_name) -> ScriptFile
  .list_drafts() -> List[str] ;  .has_draft(name) ;  .remove(name)

ScriptFile(width, height, fps=30)
  .add_track(track_type, track_name=None, *, mute=False, relative_index=0, absolute_index=None)
  .add_material(material) ;  .add_segment(segment, track_name=None)
  .import_srt(srt_path, track_name, *, time_offset=0.0, style_reference=None, text_style=..., clip_settings=...)
  .dumps() -> str ;  .dump(file_path) ;  .save()
  .load_template(json_path)  # staticmethod

VideoMaterial(path, material_name=None, crop_settings=CropSettings())
AudioMaterial(path, material_name=None)

VideoSegment(material, target_timerange, *, source_timerange=None, speed=None,
             volume=1.0, clip_settings=None)
  .add_transition(transition_type, *, duration=None)
AudioSegment(material, target_timerange, *, source_timerange=None, speed=None, volume=1.0)
TextSegment(text, timerange, *, font=None, style=None, clip_settings=None,
            border=None, background=None)

Timerange(start, duration)        # ⚠ end가 아니라 duration
ClipSettings(*, alpha=1.0, flip_horizontal=False, flip_vertical=False, rotation=0.0,
             scale_x=1.0, scale_y=1.0, transform_x=0.0, transform_y=0.0)
TextStyle(*, size=8.0, bold=False, italic=False, underline=False,
          color=(1.0,1.0,1.0), alpha=1.0, align=0, vertical=False,
          letter_spacing=0, line_spacing=0, auto_wrapping=False, max_line_width=0.82)
TextBorder(*, alpha=1.0, color=(0.0,0.0,0.0), width=40.0)
TextBackground(*, color: str, style: Literal[1,2]=1, alpha=1.0, round_radius=0.0,
               height=0.14, width=0.14, horizontal_offset=0.5, vertical_offset=0.5)
```

### 색상 표현이 타입마다 다릅니다 (요청서에 없던 함정)

| 대상 | 타입 | 예 |
|---|---|---|
| `TextStyle.color` | **float 튜플 (0~1)** | `(0.0, 0.0, 0.0)` = 검정 |
| `TextBorder.color` | **float 튜플 (0~1)** | `(1.0, 1.0, 1.0)` = 흰색 |
| `TextBackground.color` | **hex 문자열** | `"#ffffff"` |

요청서 3.4절의 실측 정상값은 *드래프트 JSON 기준*이라 `text_color=#000000`처럼 적혀 있는데,
pyCapCut 생성자에 넣을 때는 `(0.0, 0.0, 0.0)`으로 변환해야 합니다.
→ `services/capcut_draft.py`의 `hex_to_rgb_tuple()` / `rgb_tuple_to_hex()`가 담당.

### `TextBorder.width`는 100배 스케일입니다 (요청서에 없던 함정)

```python
# text_segment.py:106
self.width = width / 100.0 * 0.2   # 소스 주석: "此映射可能不完全正确"
```

요청서 3.4절의 실측값 `border_width=0.08`은 **JSON에 기록되는 값**입니다.
그 값을 얻으려면 생성자에 `width=40.0`(기본값)을 넘겨야 합니다.
`TextBorder(width=0.08)`로 그대로 넘기면 JSON에 `0.00016`이 들어가 테두리가 사실상 사라집니다.

```
TextBorder(width=40.0)  -> strokes[0].width = 0.08   ← 정상
TextBorder(width=0.08)  -> strokes[0].width = 0.00016 ← 함정
```

---

## 2. 요청서 3절 재검증 결과

| 절 | 내용 | 결과 |
|---|---|---|
| 3.1 | 드래프트 구조 / µs 정수 / `version:360000` | ✅ 확인 |
| 3.2 | 정규화 좌표, y 위쪽 양수 | ✅ 확인 (JSON `clip.transform`) |
| 3.3 | `track_render_index` | ⚠ **요청서와 다름** — 아래 참조 |
| 3.4 | `check_flag` 비트마스크 | ⚠ **0.0.3에서는 자동 처리됨** — 아래 참조 |
| 3.5 | 텍스트 필드 누락 | ✅ 확인 (소재 **20개 필드**뿐) |
| 3.6 | 폰트 절대경로 / Pretendard 없음 | ✅ 확인 (FontType 348개, Pretendard 0건) |
| 3.7 | `effect_id` 역조회 | ✅ **정확히 재현됨** |
| 3.8 | 소재 길이 1µs 초과 시 거부 | ✅ 확인 (`video_segment.py:334`) |
| 3.10 | `dumps()`가 ratio를 `"original"` 고정 | ✅ **소스에 하드코딩 확인** |
| 3.12 | `create_draft`가 레지스트리 미등록 | ✅ 확인 |

### 3.3 ⚠ `track_render_index`는 "0"이 아니라 **트랙에 아예 없습니다**

요청서는 "pyCapCut은 전부 0으로 둡니다"라고 적었지만, 0.0.3 실물은 다릅니다.

**트랙 dict에는 `track_render_index` 키 자체가 없습니다.**
```python
# track.py Track.export_json() 이 내보내는 전부:
{"attribute", "flag", "id", "is_default_name", "name", "segments", "type"}
```

**대신 각 세그먼트에 두 개의 키가 붙습니다.**
```python
# segment.py:68
"track_render_index": 0,        # ← 항상 0으로 하드코딩 (여기가 진짜 원인)
# track.py Track.export_json()
seg["render_index"] = self.render_index   # video=0, text=15000, effect=10000 ...
```

실제 생성 결과:
```
[0] type=video  track_render_index=<키 없음>  seg: render_index=0     track_render_index=0
[1] type=text   track_render_index=<키 없음>  seg: render_index=15000 track_render_index=0
```

→ **결론: 요청서의 처방(트랙 순서대로 다시 매기기)은 여전히 맞지만, 고칠 대상이 트랙이 아니라
세그먼트입니다.** `capcut_draft.fix_track_render_index()`는
① 각 트랙 dict에 `track_render_index`를 트랙 인덱스로 써 넣고
② **그 트랙의 모든 세그먼트의 `track_render_index`도 같은 값으로** 덮어씁니다.
②를 빼먹으면 요청서가 경고한 그대로 자막이 영상에 가려집니다.

### 3.4 ⚠ `check_flag`는 0.0.3이 이미 올바르게 계산합니다

```python
# text_segment.py:336
check_flag: int = 7
if <border 있음>:     check_flag |= 8
if <background 있음>: check_flag |= 16
```

`TextBorder`와 `TextBackground`를 **둘 다 넘기면** `check_flag=31`이 자동으로 들어갑니다.
실제 생성물 확인: `check_flag = 31`, `background_style = 1`. 요청서 실측값과 일치합니다.
또 `TextBackground.style`은 타입이 `Literal[1, 2]`라 **0을 넣는 것 자체가 불가능**합니다.

→ 즉 **pyCapCut 경로로 만들면 3.4는 이미 안전합니다.**
다만 캘리브레이션 경로에서는 캡컷 원본 소재 dict를 통째로 이식하므로,
그때 `check_flag`/`background_style`이 살아 있는지 **저장 후 검증**해야 합니다.
`verify_subtitle_visibility()`가 이 두 값을 다시 읽어 확인합니다.

### 3.5 텍스트 소재는 **20개 필드**뿐입니다

pyCapCut이 만든 `materials.texts[0]`의 전체 키:
```
alignment, background_alpha, background_color, background_height,
background_horizontal_offset, background_round_radius, background_style,
background_vertical_offset, background_width, check_flag, content,
force_apply_line_max_width, global_alpha, id, letter_spacing, line_feed,
line_max_width, line_spacing, type, typesetting
```
`words`, `shadow_*`, `border_mode`, `layer_weight`, `font_path`, `font_size`,
`font_name` 등은 **소스에 주석 처리된 채로 존재**합니다 (`text_segment.py:411~422`).

→ 요청서 처방대로 **캘리브레이션에서 캡컷 원본 소재를 통째로 저장 → 텍스트만 갈아끼우기**.
`capcut_draft.graft_text_material()`이 담당합니다.

### 3.6 폰트 — 소스에서 더미 경로 확인

```python
# text_segment.py:366
content_json["styles"][0]["font"] = {
    "id": self.font.resource_id,
    "path": "C:/%s.ttf" % self.font.name   # 주석: "并不会真正在此处放置字体文件"
}
```
`FontType` 348개 전수 검색 결과 **Pretendard 0건**. → 후처리로 절대경로 주입 필수.

### 3.7 `effect_id` 역조회 — 요청서 예시 그대로 재현됨

```
TransitionType 멤버 1137개, effect_id 중복 0건 (역조회 안전)
6725771847444468236  ->  TransitionType.故障   ← 요청서 예시와 정확히 일치
```
이름은 중국어(`故障`, `泛光`, `闪白`)와 영어(`Cutout_Flip`, `White_Flash`)가 섞여 있어
이름 매칭은 여전히 불가. **`effect_id` 역조회만 사용합니다.**

### 3.8 길이 검사 위치 확인

```python
# video_segment.py:334 / audio_segment.py:149
if source_timerange.end > material.duration:
    raise ValueError(f"截取的素材时间范围 {source_timerange} 超出了素材时长({material.duration})")
```
`material.duration` 산출식:
```python
# local_materials.py:100
self.duration = int(info.video_tracks[0].duration * 1e3)   # pymediainfo는 ms 단위
```
→ **ms에서 잘린 뒤 1000배**. 그래서 ffprobe의 `437.766667s`가 `437766000µs`가 됩니다.
`builder.py`는 세그먼트 생성 전 `VideoMaterial.duration`을 읽어 구간을 그 값으로 clamp합니다.

### 이미지 판정 (요청서 3차 단계용) — 실측 확인

```python
# local_materials.py
elif len(info.image_tracks):
    self.material_type = "photo"
    self.duration = 10800000000   # 3시간
```
→ `materials.videos[].type == "photo"` 1차 판정이 맞습니다. 확장자 교차검증 병행.

---

## 3. 드래프트 폴더 자동 탐지 (하드코딩 금지)

`config.py`의 `find_capcut_draft_root()`가 아래 순서로 탐색합니다.
**소스에 경로를 박지 않고 전부 런타임 탐지**하며, 사용자가 설정으로 덮어쓸 수 있습니다.

1. `data/settings.json`의 `draft_root` (사용자 지정이 최우선)
2. 환경변수 `CAPCUT_DRAFT_ROOT`
3. `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`
4. `%APPDATA%`, `%USERPROFILE%` 하위의 동일 상대경로
5. 모든 고정 드라이브에서 `*/CapCut/User Data/Projects/com.lveditor.draft` 제한 깊이 탐색

`com.lveditor.cloud.draft_*`(클라우드)와 `com.lveditor.textTemplate.draft`(템플릿)는
이름 기준으로 **명시적으로 제외**합니다. (`EXCLUDED_DRAFT_DIR_PREFIXES`)

탐지 성공 판정은 폴더 존재가 아니라 **`root_meta_info.json` 존재 여부**로 합니다.

---

## 4. 이 환경에서 검증하지 못한 것 (README에도 동일하게 기재)

조사·개발은 Linux 컨테이너에서 이뤄졌습니다. 아래는 **Windows 실기 검증이 필요**합니다.

| 항목 | 상태 | 이유 |
|---|---|---|
| 캡컷 실기 열람 (자막 육안 확인) | **미검증** | 캡컷이 Windows 전용, 이 환경에 없음 |
| 드래프트 폴더 자동 탐지 실제 경로 | **미검증** | `%LOCALAPPDATA%` 없음. 로직·제외규칙만 검증 |
| `root_meta_info.json` 등록 후 목록 노출 | **미검증** | 실제 캡컷 필요 |
| 캡컷 프로세스 감지 (`CapCut`/`JianyingPro`) | **미검증** | 프로세스명 매칭 로직만 단위테스트 |
| 자동 내보내기 (`jianying_controller`) | **미검증** | `uiautomation` 필요(Windows 전용). **기본 OFF** |
| bat 파일 CP949 동작 | **부분검증** | 인코딩·바이트는 검증, cmd 실행은 미검증 |
| 한글 경로 cp949 깨짐 (3.15) | **부분검증** | 3중 방어 구현 + 단위테스트. 실기 재현은 미검증 |
| ffmpeg stderr 데드락 (3.14) | **✅ 검증** | 아래 참조 |

### 3.14는 이 환경에서 재현·실측했고, **요청서보다 더 나쁜 결과**가 나왔습니다

실제 인코딩 1회(`-i talk.mp4 -c:v libx264 -t 2`)에서 ffmpeg이 stderr로 내보낸 바이트 수:

| 조합 | stderr 바이트 | 윈도우 파이프 버퍼(4096) 대비 |
|---|---|---|
| 기본 (배너 포함) | **5,827** | **초과 → 데드락** |
| `-hide_banner` 만 | **4,308** | **여전히 초과 → 데드락** |
| `-hide_banner` + `-loglevel error` | **0** | 안전 |

> 측정 빌드: ffmpeg 7.0.2 static (linux x86_64).
> 요청서의 4,133바이트는 Gyan.FFmpeg 9.0(Windows) 기준이라 절대값은 다르지만,
> **결론은 같고 오히려 더 강해집니다.**

**요청서 3.14 표의 `-hide_banner` + `stderr=PIPE` = 정상 은 이 빌드에서 성립하지 않습니다.**
`-hide_banner`를 붙여도 4,308바이트로 4,096을 넘겨 그대로 막힙니다.
요청서 본문이 "`-hide_banner`만으로는 스트림이 많은 파일에서 다시 넘칠 수 있습니다"라고
경고한 그 상황이, 스트림 2개짜리 평범한 파일에서 이미 발생합니다.

→ 그래서 **세 가지를 전부** 적용했습니다.
① 모든 ffmpeg 호출에 `-hide_banner`
② 진행률이 필요한 호출은 `-loglevel error`까지 붙임 (`run_ffmpeg`)
③ `stderr`를 **별도 데몬 스레드로 계속 비움** (`audio_analysis._drain()`)

③이 최종 방어선입니다. `silencedetect`는 결과를 **stderr의 info 레벨로** 내보내기 때문에
거기서는 `-loglevel error`를 쓸 수 없고, 오직 ③만으로 막아야 합니다
(`detect_silence`가 별도 수집 스레드를 쓰는 이유).

### 무음 감지 정확도도 검증했습니다

3초 소리 / 1.5초 무음 / 2.5초 소리 / 2초 무음 / 3초 소리로 만든 wav에 대해:

```
감지 결과: [(3.02, 4.50), (7.01, 9.01)]
설계 값  : [(3.00, 4.50), (7.00, 9.00)]
```

시작점이 **+0.02초 늦게** 잡히는 것이 요청서 3.17이 말한 현상입니다.
`silencedetect`는 소리가 임계값 아래로 *떨어진 뒤*를 무음 시작으로 보므로,
말끝의 자음·여운은 이미 그 아래에 있습니다. 그래서 꼬리 여유(`tail_pad`)를
머리 여유(`head_pad`)보다 크게(0.35 vs 0.15) 잡습니다.
