#!/usr/bin/env python3
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from hashlib import sha1
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

import requests
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from ytmusicapi import YTMusic

PROXY_URL = "http://127.0.0.1:2080"
BOT_API_BASE = "https://api.telegram.org"
ALLOWED_USER_ID = 517539052
SEARCH_LIMIT = 10
CONFIDENCE_MIN = 0.60
YTM_URL = "https://music.youtube.com/watch?v={vid}"
YT_URL = "https://www.youtube.com/watch?v={vid}"
YOUTUBE_CLIENTS = ["ios", "tv_embedded", "webremix"]
POLL_TIMEOUT_S = 30
STARTED_AT = time.time()
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
GCAL_SYNC_SOURCE = "tgmusic_schedule_sync"
FORECAST_DAYS = 30
SHIFT_COLOR_IDS = {
	"16:00 - 20:00": "9",
	"06:00 - 16:00": "10",
	"20:00 - 06:00": "6",
}
SHIFT_CYCLE = [
	"16:00 - 20:00",
	"06:00 - 16:00",
	"20:00 - 06:00",
	"Выходной",
	"Выходной",
]
SCHEDULE_DATE_RE = re.compile(r"^📅\s*(\d{4}-\d{2}-\d{2})\s*$")
SCHEDULE_ENTRY_RE = re.compile(
	r"^\*\s*(?:\[(?P<name>[^\]]+)\]\([^)]+\)|(?P<plain_name>.+?))\s*—\s*(?P<shift>.+?)\s*$"
)
SCHEDULE_TIME_RE = re.compile(r"^(?P<start>\d{2}:\d{2})\s*-\s*(?P<end>\d{2}:\d{2})$")
SD_INFO_RE = re.compile(r"Информация из SD:\s*(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})")

PENALTY_TERMS = {
	"live",
	"remix",
	"cover",
	"sped",
	"slowed",
	"nightcore",
	"8d",
	"reverb",
	"extended",
	"mashup",
	"edit",
	"karaoke",
	"instrumental",
	"demo",
	"tribute",
	"soundalike",
}


@dataclass
class PendingChoice:
	query: str
	candidates: List[Dict]
	created_at: float


@dataclass
class ScheduleShift:
	employee: str
	shift_date: date
	start_at: datetime
	end_at: datetime
	raw_shift: str

	@property
	def schedule_key(self) -> str:
		return f"{self.shift_date.isoformat()}|{self.employee}|{self.raw_shift}"


@dataclass
class ScheduleDay:
	employee: str
	shift_date: date
	raw_shift: str


PENDING_BY_CHAT: Dict[int, PendingChoice] = {}


def configure_proxy() -> None:
	os.environ["HTTP_PROXY"] = PROXY_URL
	os.environ["HTTPS_PROXY"] = PROXY_URL
	os.environ["ALL_PROXY"] = PROXY_URL
	os.environ["http_proxy"] = PROXY_URL
	os.environ["https_proxy"] = PROXY_URL
	os.environ["all_proxy"] = PROXY_URL


def load_dotenv() -> None:
	candidates = [
		pathlib.Path(__file__).resolve().parent / ".env",
		pathlib.Path(__file__).resolve().parent.parent / ".env",
		pathlib.Path.cwd() / ".env",
	]
	for path in candidates:
		if not path.exists():
			continue
		for raw_line in path.read_text(encoding="utf-8").splitlines():
			line = raw_line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			key, value = line.split("=", 1)
			key = key.strip()
			value = value.strip().strip("'").strip('"')
			if key and key not in os.environ:
				os.environ[key] = value
		return


def bot_token() -> str:
	token = os.environ.get("TG_BOT_TOKEN", "").strip()
	if not token:
		raise RuntimeError("Set TG_BOT_TOKEN in .env or in the environment before starting the bot.")
	return token


def session() -> requests.Session:
	s = requests.Session()
	s.proxies.update({"http": PROXY_URL, "https": PROXY_URL})
	return s


HTTP = session()


def api_url(method: str) -> str:
	return f"{BOT_API_BASE}/bot{bot_token()}/{method}"


def file_api_url(file_token: str) -> str:
	return f"{BOT_API_BASE}/file/bot{bot_token()}/{file_token}"


def api_call(method: str, *, data=None, files=None, timeout: int = 60) -> Dict:
	resp = HTTP.post(api_url(method), data=data, files=files, timeout=timeout)
	resp.raise_for_status()
	payload = resp.json()
	if not payload.get("ok"):
		raise RuntimeError(f"Telegram API error in {method}: {payload}")
	return payload["result"]


def get_updates(offset: Optional[int]) -> List[Dict]:
	resp = HTTP.get(
		api_url("getUpdates"),
		params={
			"offset": offset,
			"timeout": POLL_TIMEOUT_S,
			"allowed_updates": json.dumps(["message", "callback_query"]),
		},
		timeout=POLL_TIMEOUT_S + 10,
	)
	resp.raise_for_status()
	payload = resp.json()
	if not payload.get("ok"):
		raise RuntimeError(f"Telegram API error in getUpdates: {payload}")
	return payload["result"]


def send_message(chat_id: int, text: str, reply_markup: Optional[Dict] = None) -> None:
	data = {"chat_id": str(chat_id), "text": text}
	if reply_markup is not None:
		data["reply_markup"] = json.dumps(reply_markup)
	api_call("sendMessage", data=data)


def send_chat_action(chat_id: int, action: str) -> None:
	api_call("sendChatAction", data={"chat_id": str(chat_id), "action": action})


def answer_callback_query(callback_query_id: str, text: Optional[str] = None, show_alert: bool = False) -> None:
	data = {"callback_query_id": callback_query_id}
	if text:
		data["text"] = text
	if show_alert:
		data["show_alert"] = "true"
	api_call("answerCallbackQuery", data=data)


def edit_message_reply_markup(chat_id: int, message_id: int, reply_markup: Optional[Dict] = None) -> None:
	data = {"chat_id": str(chat_id), "message_id": str(message_id)}
	if reply_markup is not None:
		data["reply_markup"] = json.dumps(reply_markup)
	api_call("editMessageReplyMarkup", data=data)


def send_audio(chat_id: int, path: pathlib.Path, title: str, artist: str, cover_bytes: Optional[bytes]) -> None:
	data = {"chat_id": str(chat_id), "title": title, "performer": artist}
	files = {"audio": (path.name, path.open("rb"), "audio/mp4")}
	thumb_handle = None
	try:
		if cover_bytes:
			thumb_handle = io.BytesIO(cover_bytes)
			thumb_handle.name = "cover.jpg"
			files["thumbnail"] = ("cover.jpg", thumb_handle, "image/jpeg")
		api_call("sendAudio", data=data, files=files, timeout=300)
	finally:
		audio_handle = files["audio"][1]
		audio_handle.close()
		if thumb_handle is not None:
			thumb_handle.close()


def send_video(chat_id: int, path: pathlib.Path, caption: Optional[str] = None) -> None:
	data = {"chat_id": str(chat_id), "supports_streaming": "true"}
	if caption:
		data["caption"] = caption[:1024]
	files = {"video": (path.name, path.open("rb"), "video/mp4")}
	try:
		api_call("sendVideo", data=data, files=files, timeout=300)
	finally:
		files["video"][1].close()


def send_photo(chat_id: int, path: pathlib.Path, caption: Optional[str] = None) -> None:
	data = {"chat_id": str(chat_id)}
	if caption:
		data["caption"] = caption[:1024]
	files = {"photo": (path.name, path.open("rb"), "image/jpeg")}
	try:
		api_call("sendPhoto", data=data, files=files, timeout=300)
	finally:
		files["photo"][1].close()


def send_document(chat_id: int, path: pathlib.Path, caption: Optional[str] = None) -> None:
	data = {"chat_id": str(chat_id)}
	if caption:
		data["caption"] = caption[:1024]
	files = {"document": (path.name, path.open("rb"), "application/octet-stream")}
	try:
		api_call("sendDocument", data=data, files=files, timeout=300)
	finally:
		files["document"][1].close()


def send_media_group(chat_id: int, paths: List[pathlib.Path], caption: Optional[str] = None) -> None:
	data = {"chat_id": str(chat_id)}
	files = {}
	handles = []
	media = []
	try:
		for index, path in enumerate(paths):
			kind = social_media_kind(path)
			attach_name = f"media{index}"
			handle = path.open("rb")
			handles.append(handle)
			mime = "video/mp4" if kind == "video" else "image/jpeg"
			files[attach_name] = (path.name, handle, mime)
			item = {
				"type": "video" if kind == "video" else "photo",
				"media": f"attach://{attach_name}",
			}
			if kind == "video":
				item["supports_streaming"] = True
			if caption and index == 0:
				item["caption"] = caption[:1024]
			media.append(item)
		data["media"] = json.dumps(media)
		api_call("sendMediaGroup", data=data, files=files, timeout=300)
	finally:
		for handle in handles:
			handle.close()


def google_calendar_id() -> str:
	return os.environ.get("GOOGLE_CALENDAR_ID", "primary").strip() or "primary"


def google_calendar_timezone() -> str:
	return os.environ.get("GOOGLE_CALENDAR_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"


def calendar_zoneinfo() -> ZoneInfo:
	return ZoneInfo(google_calendar_timezone())


def google_access_token() -> str:
	client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
	client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
	refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()
	missing = [
		name
		for name, value in (
			("GOOGLE_CLIENT_ID", client_id),
			("GOOGLE_CLIENT_SECRET", client_secret),
			("GOOGLE_REFRESH_TOKEN", refresh_token),
		)
		if not value
	]
	if missing:
		raise RuntimeError("Missing Google Calendar settings: " + ", ".join(missing))
	resp = HTTP.post(
		GOOGLE_TOKEN_URL,
		data={
			"client_id": client_id,
			"client_secret": client_secret,
			"refresh_token": refresh_token,
			"grant_type": "refresh_token",
		},
		timeout=30,
	)
	resp.raise_for_status()
	payload = resp.json()
	access_token = str(payload.get("access_token") or "").strip()
	if not access_token:
		raise RuntimeError(f"Google token response has no access_token: {payload}")
	return access_token


def google_calendar_request(
	method: str,
	path: str,
	*,
	params: Optional[Dict] = None,
	json_body: Optional[Dict] = None,
) -> Dict:
	resp = HTTP.request(
		method,
		f"{GOOGLE_CALENDAR_API_BASE}/{path.lstrip('/')}",
		params=params,
		json=json_body,
		headers={
			"Authorization": f"Bearer {google_access_token()}",
			"Accept": "application/json",
		},
		timeout=60,
	)
	resp.raise_for_status()
	if resp.status_code == 204 or not resp.content:
		return {}
	return resp.json()


def is_schedule_message(text: str) -> bool:
	return "🗓" in text and "Расписание" in text and "📅" in text


def build_shift(employee: str, shift_date: date, shift_text: str) -> ScheduleShift:
	time_match = SCHEDULE_TIME_RE.match(shift_text)
	if not time_match:
		raise ValueError(f"Unsupported shift format: {shift_text}")
	start_clock = dt_time.fromisoformat(time_match.group("start"))
	end_clock = dt_time.fromisoformat(time_match.group("end"))
	start_at = datetime.combine(shift_date, start_clock)
	end_at = datetime.combine(shift_date, end_clock)
	if end_at <= start_at:
		end_at += timedelta(days=1)
	return ScheduleShift(
		employee=employee,
		shift_date=shift_date,
		start_at=start_at,
		end_at=end_at,
		raw_shift=shift_text,
	)


def parse_schedule_message(text: str) -> Tuple[List[ScheduleShift], int, Optional[str], List[date], List[ScheduleDay]]:
	shifts: List[ScheduleShift] = []
	days: List[ScheduleDay] = []
	current_date: Optional[date] = None
	off_days = 0
	seen_dates: List[date] = []
	info_match = SD_INFO_RE.search(text)
	source_info = info_match.group(1) if info_match else None

	for raw_line in text.splitlines():
		line = raw_line.strip()
		if not line:
			continue
		date_match = SCHEDULE_DATE_RE.match(line)
		if date_match:
			current_date = date.fromisoformat(date_match.group(1))
			seen_dates.append(current_date)
			continue
		entry_match = SCHEDULE_ENTRY_RE.match(line)
		if not entry_match:
			continue
		if current_date is None:
			raise ValueError("Found a shift row before any date row.")
		employee = (entry_match.group("name") or entry_match.group("plain_name") or "").strip()
		shift_text = entry_match.group("shift").strip()
		if not employee:
			raise ValueError(f"Could not parse employee name in line: {line}")
		days.append(ScheduleDay(employee=employee, shift_date=current_date, raw_shift=shift_text))
		if shift_text.casefold() == "выходной":
			off_days += 1
			continue
		shifts.append(build_shift(employee, current_date, shift_text))

	if not shifts and off_days == 0:
		raise ValueError("No schedule rows were found in the message.")
	return shifts, off_days, source_info, seen_dates, days


def schedule_event_id(schedule_key: str) -> str:
	return f"tgshift{sha1(schedule_key.encode('utf-8')).hexdigest()[:28]}"


def schedule_shift_color_id(raw_shift: str) -> Optional[str]:
	return SHIFT_COLOR_IDS.get(raw_shift)


def infer_cycle_position(days: List[ScheduleDay]) -> int:
	if not days:
		raise ValueError("Cannot infer shift cycle from an empty schedule.")
	ordered_days = sorted(days, key=lambda item: item.shift_date)
	statuses = [item.raw_shift for item in ordered_days]
	best_index = 0
	best_score = -1
	for candidate_index, cycle_value in enumerate(SHIFT_CYCLE):
		if cycle_value != statuses[-1]:
			continue
		score = 0
		for offset, status in enumerate(reversed(statuses)):
			if SHIFT_CYCLE[(candidate_index - offset) % len(SHIFT_CYCLE)] != status:
				break
			score += 1
		if score > best_score:
			best_score = score
			best_index = candidate_index
	if best_score <= 0:
		raise ValueError("Could not align the provided schedule with the expected work cycle.")
	return best_index


def extend_schedule_with_forecast(days: List[ScheduleDay]) -> List[ScheduleDay]:
	if not days:
		return []
	ordered_days = sorted(days, key=lambda item: item.shift_date)
	last_day = ordered_days[-1]
	cycle_position = infer_cycle_position(ordered_days)
	extended = list(ordered_days)
	for step in range(1, FORECAST_DAYS + 1):
		next_date = last_day.shift_date + timedelta(days=step)
		next_status = SHIFT_CYCLE[(cycle_position + step) % len(SHIFT_CYCLE)]
		extended.append(
			ScheduleDay(
				employee=last_day.employee,
				shift_date=next_date,
				raw_shift=next_status,
			)
		)
	return extended


def build_forecast_shifts(days: List[ScheduleDay]) -> List[ScheduleShift]:
	shifts: List[ScheduleShift] = []
	for day in days:
		if day.raw_shift.casefold() == "выходной":
			continue
		shifts.append(build_shift(day.employee, day.shift_date, day.raw_shift))
	return shifts


def schedule_event_payload(shift: ScheduleShift, source_info: Optional[str]) -> Dict:
	timezone = google_calendar_timezone()
	start_at = shift.start_at.replace(tzinfo=calendar_zoneinfo())
	end_at = shift.end_at.replace(tzinfo=calendar_zoneinfo())
	color_id = schedule_shift_color_id(shift.raw_shift)
	description_lines = [
		f"Employee: {shift.employee}",
		f"Shift date: {shift.shift_date.isoformat()}",
		f"Shift hours: {shift.raw_shift}",
		"Imported from Telegram schedule message.",
	]
	if source_info:
		description_lines.append(f"SD info: {source_info}")
	payload = {
		"id": schedule_event_id(shift.schedule_key),
		"summary": f"Смена: {shift.employee}",
		"description": "\n".join(description_lines),
		"start": {"dateTime": start_at.isoformat(), "timeZone": timezone},
		"end": {"dateTime": end_at.isoformat(), "timeZone": timezone},
		"extendedProperties": {
			"private": {
				"source": GCAL_SYNC_SOURCE,
				"schedule_key": shift.schedule_key,
				"employee": shift.employee,
				"raw_shift": shift.raw_shift,
				"source_info": source_info or "",
			}
		},
	}
	if color_id:
		payload["colorId"] = color_id
	return payload


def list_calendar_events(date_from: datetime, date_to: datetime) -> List[Dict]:
	calendar_id = quote(google_calendar_id(), safe="")
	zone = calendar_zoneinfo()
	start_value = date_from.replace(tzinfo=zone).isoformat()
	end_value = date_to.replace(tzinfo=zone).isoformat()
	page_token: Optional[str] = None
	items: List[Dict] = []
	while True:
		params = {
			"timeMin": start_value,
			"timeMax": end_value,
			"singleEvents": "true",
			"showDeleted": "false",
			"maxResults": "2500",
		}
		if page_token:
			params["pageToken"] = page_token
		payload = google_calendar_request("GET", f"calendars/{calendar_id}/events", params=params)
		items.extend(payload.get("items", []))
		page_token = payload.get("nextPageToken")
		if not page_token:
			return items


def sync_schedule_to_google_calendar(
	shifts: List[ScheduleShift],
	source_info: Optional[str],
	schedule_dates: List[date],
) -> Dict[str, int]:
	if schedule_dates:
		range_start = datetime.combine(min(schedule_dates), dt_time.min) - timedelta(days=1)
		range_end = datetime.combine(max(schedule_dates), dt_time.max) + timedelta(days=1)
	elif shifts:
		range_start = min(shift.start_at for shift in shifts) - timedelta(days=1)
		range_end = max(shift.end_at for shift in shifts) + timedelta(days=1)
	else:
		return {"created": 0, "updated": 0, "deleted": 0}
	all_events = list_calendar_events(range_start, range_end)
	existing_events = []
	for item in all_events:
		private_props = (((item.get("extendedProperties") or {}).get("private")) or {})
		if private_props.get("source") == GCAL_SYNC_SOURCE:
			existing_events.append(item)
	desired_by_key = {shift.schedule_key: shift for shift in shifts}
	existing_by_key: Dict[str, List[Dict]] = {}
	for item in existing_events:
		private_props = (((item.get("extendedProperties") or {}).get("private")) or {})
		schedule_key = str(private_props.get("schedule_key") or "").strip()
		if not schedule_key:
			continue
		existing_by_key.setdefault(schedule_key, []).append(item)

	calendar_id = quote(google_calendar_id(), safe="")
	created = 0
	updated = 0
	deleted = 0

	for schedule_key, shift in desired_by_key.items():
		payload = schedule_event_payload(shift, source_info)
		current_items = existing_by_key.get(schedule_key, [])
		if not current_items:
			google_calendar_request("POST", f"calendars/{calendar_id}/events", json_body=payload)
			created += 1
			continue
		primary = current_items[0]
		for duplicate in current_items[1:]:
			duplicate_id = duplicate.get("id")
			if duplicate_id:
				google_calendar_request("DELETE", f"calendars/{calendar_id}/events/{quote(str(duplicate_id), safe='')}")
				deleted += 1
		private_props = (((primary.get("extendedProperties") or {}).get("private")) or {})
		needs_update = any(
			(
				(primary.get("summary") or "") != payload["summary"],
				((primary.get("start") or {}).get("dateTime") or "") != payload["start"]["dateTime"],
				((primary.get("end") or {}).get("dateTime") or "") != payload["end"]["dateTime"],
				((primary.get("description") or "") != payload["description"]),
				str(primary.get("colorId") or "") != str(payload.get("colorId") or ""),
				private_props.get("source_info", "") != (source_info or ""),
			)
		)
		if needs_update:
			google_calendar_request(
				"PUT",
				f"calendars/{calendar_id}/events/{quote(str(primary['id']), safe='')}",
				json_body=payload,
			)
			updated += 1

	for schedule_key, current_items in existing_by_key.items():
		if schedule_key in desired_by_key:
			continue
		for item in current_items:
			event_id = item.get("id")
			if event_id:
				google_calendar_request("DELETE", f"calendars/{calendar_id}/events/{quote(str(event_id), safe='')}")
				deleted += 1

	return {"created": created, "updated": updated, "deleted": deleted}


def handle_schedule_message(chat_id: int, text: str) -> None:
	send_chat_action(chat_id, "typing")
	shifts, off_days, source_info, schedule_dates, days = parse_schedule_message(text)
	extended_days = extend_schedule_with_forecast(days)
	forecast_days = extended_days[len(days):]
	forecast_shifts = build_forecast_shifts(forecast_days)
	all_shifts = shifts + forecast_shifts
	all_dates = schedule_dates + [day.shift_date for day in forecast_days]
	stats = sync_schedule_to_google_calendar(all_shifts, source_info, all_dates)
	first_day = min(schedule_dates) if schedule_dates else None
	last_day = max(all_dates) if all_dates else None
	period = f"{first_day.isoformat()} .. {last_day.isoformat()}" if first_day and last_day else "n/a"
	send_message(
		chat_id,
		"Расписание синхронизировано с Google Calendar.\n"
		f"Период: {period}\n"
		f"Смен из сообщения: {len(shifts)}\n"
		f"Автодобавлено вперёд: {len(forecast_shifts)}\n"
		f"Выходных пропущено: {off_days}\n"
		f"Создано: {stats['created']}\n"
		f"Обновлено: {stats['updated']}\n"
		f"Удалено старых: {stats['deleted']}",
	)


def toks(text: str) -> Set[str]:
	return set(re.findall(r"[^\W_]+", (text or "").lower(), flags=re.UNICODE))


def overlap_ratio(needle: Set[str], haystack: Set[str]) -> float:
	return len(needle & haystack) / max(1, len(needle))


def duration_s(value: Optional[int | str]) -> int:
	try:
		if value is None:
			return 0
		if isinstance(value, str):
			text = value.strip()
			if not text:
				return 0
			if ":" in text:
				total = 0
				for part in text.split(":"):
					total = total * 60 + int(part)
				return total
			return int(float(text))
		return int(value)
	except Exception:
		return 0


def clean_query(search_query: str) -> str:
	query = search_query.strip()
	query = re.sub(r"\s+", " ", query)
	query = query.replace(" - ", " ")
	return query


def strip_noise(search_query: str) -> str:
	text = re.sub(r"[\(\[][^\)\]]*[Ff]eat[^\)\]]*[\)\]]", " ", search_query)
	text = re.sub(r"[\(\[][Oo]fficial[^\)\]]*[\)\]]", " ", text)
	text = re.sub(r"[\(\[][Ll]ive[^\)\]]*[\)\]]", " ", text)
	text = re.sub(r"[\(\[][Rr]emix[^\)\]]*[\)\]]", " ", text)
	text = re.sub(r"[\(\)\[\]]", " ", text)
	return re.sub(r"\s+", " ", text).strip()


def query_variants(search_query: str) -> List[str]:
	base = clean_query(search_query)
	cleaned = clean_query(strip_noise(search_query))
	variants = [base]
	if cleaned and cleaned != base:
		variants.append(cleaned)
	if "&" in base:
		variants.append(base.replace("&", "and"))
	if re.search(r"\band\b", base, flags=re.I):
		variants.append(re.sub(r"\band\b", "&", base, flags=re.I))

	seen = set()
	out = []
	for item in variants:
		value = re.sub(r"\s+", " ", item).strip()
		if value and value not in seen:
			seen.add(value)
			out.append(value)
	return out


def candidate_artist_text(candidate: Dict) -> str:
	artists = candidate.get("artists")
	if artists:
		try:
			return ", ".join(artist.get("name", "") for artist in artists)
		except Exception:
			pass
	return candidate.get("author", "") or ""


def score_candidate(search_query: str, candidate: Dict) -> float:
	query_tokens = toks(search_query)
	candidate_title = candidate.get("title") or ""
	candidate_artist = candidate_artist_text(candidate)
	title_overlap = overlap_ratio(query_tokens, toks(candidate_title))
	artist_overlap = overlap_ratio(query_tokens, toks(candidate_artist))
	yt_duration = duration_s(candidate.get("duration_seconds"))
	duration_score = 0.7 if yt_duration <= 0 else 1.0

	channel = (candidate.get("author") or "").lower()
	channel_boost = 0.15 if ("topic" in channel or "official" in channel) else 0.0

	blob = f"{candidate_title} {candidate_artist}".lower()
	penalty = 0.0
	for term in PENALTY_TERMS:
		if term in blob:
			penalty += 0.10

	total = duration_score * 0.30 + title_overlap * 0.40 + artist_overlap * 0.25 + channel_boost - penalty
	return max(0.0, min(total, 0.99))


def search_filter(yt: YTMusic, query: str, search_filter_name: str, limit: int) -> List[Dict]:
	results = yt.search(query, filter=search_filter_name, limit=limit) or []
	candidates: List[Dict] = []
	source = "music" if search_filter_name == "songs" else "videos"
	for item in results:
		video_id = item.get("videoId")
		if not video_id:
			continue
		artists = item.get("artists")
		candidates.append({
			"videoId": video_id,
			"title": item.get("title"),
			"artists": artists if search_filter_name == "songs" else None,
			"author": (artists[0]["name"] if artists and search_filter_name == "songs" else item.get("author") or ""),
			"duration_seconds": duration_s(item.get("duration_seconds") or item.get("duration")),
			"source": source,
		})
	return candidates


def find_best(search_query: str) -> Tuple[Optional[Dict], float, List[Dict]]:
	yt = YTMusic()
	all_candidates: List[Dict] = []
	seen_ids: Set[str] = set()

	for query in query_variants(search_query):
		for search_filter_name in ("songs", "videos"):
			for candidate in search_filter(yt, query, search_filter_name, SEARCH_LIMIT):
				video_id = candidate.get("videoId")
				if not video_id or video_id in seen_ids:
					continue
				seen_ids.add(video_id)
				candidate["score"] = score_candidate(search_query, candidate)
				all_candidates.append(candidate)

	ranked = sorted(
		all_candidates,
		key=lambda item: (item["score"], 1 if item.get("source") == "music" else 0),
		reverse=True,
	)
	if not ranked:
		return None, 0.0, []

	best = ranked[0]
	if best["score"] < CONFIDENCE_MIN:
		return None, best["score"], ranked
	return best, best["score"], ranked


def sanitize_name(name: str) -> str:
	cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name or "")
	cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
	return cleaned[:120] or "file"


def is_youtube_url(text: str) -> bool:
	try:
		parsed = urlparse(text.strip())
	except Exception:
		return False
	host = (parsed.netloc or "").lower()
	return any(domain in host for domain in ("youtube.com", "youtu.be"))


def is_social_video_url(text: str) -> bool:
	try:
		parsed = urlparse(text.strip())
	except Exception:
		return False
	host = (parsed.netloc or "").lower()
	path = parsed.path or ""
	if any(domain in host for domain in ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com")):
		return True
	if "instagram.com" in host and (
		path.startswith("/reel/")
		or path.startswith("/reels/")
		or path.startswith("/p/")
		or path.startswith("/tv/")
	):
		return True
	return False


def extract_youtube_video_id(url: str) -> Optional[str]:
	try:
		parsed = urlparse(url.strip())
	except Exception:
		return None

	host = (parsed.netloc or "").lower()
	path = parsed.path or ""

	if "youtu.be" in host:
		candidate = path.strip("/").split("/")[0]
		return candidate or None

	if "youtube.com" in host:
		if path == "/watch":
			query = parse_qs(parsed.query or "")
			candidate = (query.get("v") or [None])[0]
			return candidate or None
		if path.startswith("/shorts/") or path.startswith("/embed/") or path.startswith("/live/"):
			parts = [part for part in path.split("/") if part]
			if len(parts) >= 2:
				return parts[1]
	return None


def probe_video_metadata(url: str, video_id: str) -> Tuple[str, str]:
	yt_bin = ytdlp_path()
	cmd = [
		yt_bin,
		"--proxy",
		PROXY_URL,
		"--dump-single-json",
		"--no-playlist",
		url,
	]
	proc = subprocess.run(cmd, capture_output=True, text=True)
	if proc.returncode != 0:
		return f"YouTube {video_id}", "YouTube"

	try:
		payload = json.loads(proc.stdout)
	except Exception:
		return f"YouTube {video_id}", "YouTube"

	title = (payload.get("track") or payload.get("title") or f"YouTube {video_id}").strip()
	artist = (
		payload.get("artist")
		or payload.get("uploader")
		or payload.get("channel")
		or "YouTube"
	)
	return str(title).strip() or f"YouTube {video_id}", str(artist).strip() or "YouTube"


def yt_thumbnail_bytes(video_id: str) -> Optional[bytes]:
	for quality in ("maxresdefault", "sddefault", "hqdefault", "mqdefault", "default"):
		url = f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg"
		try:
			response = HTTP.get(url, timeout=20)
			if response.status_code == 200 and response.content and len(response.content) > 1024:
				return response.content
		except Exception:
			pass
	return None


def tag_file(path: pathlib.Path, title: str, artist: str, cover_bytes: Optional[bytes]) -> None:
	if path.suffix.lower() == ".mp3":
		try:
			_ = EasyID3(path)
		except Exception:
			try:
				EasyID3.register_text_key("date", "TDRC")
			except Exception:
				pass
			audio = EasyID3()
			audio.save(path)

		audio = EasyID3(path)
		audio["title"] = title
		audio["artist"] = artist
		audio.save()

		if cover_bytes:
			id3 = ID3(path)
			id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
			id3.save(v2_version=3)
		return

	if path.suffix.lower() in {".m4a", ".mp4"}:
		audio = MP4(path)
		audio["\xa9nam"] = [title]
		audio["\xa9ART"] = [artist]
		if cover_bytes:
			audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
		audio.save()


def ytdlp_path() -> str:
	root = pathlib.Path(__file__).resolve().parent.parent
	local = root / "yt-dlp"
	if local.exists():
		return str(local)
	return "yt-dlp"


def format_uptime(seconds: float) -> str:
	total = max(0, int(seconds))
	hours, rem = divmod(total, 3600)
	minutes, secs = divmod(rem, 60)
	if hours:
		return f"{hours}h {minutes}m {secs}s"
	if minutes:
		return f"{minutes}m {secs}s"
	return f"{secs}s"


def probe_ytdlp_status() -> str:
	try:
		proc = subprocess.run(
			[ytdlp_path(), "--version"],
			capture_output=True,
			text=True,
			timeout=10,
		)
	except Exception as exc:
		return f"error: {exc}"
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout or "").strip()
		return f"error: {detail or f'exit {proc.returncode}'}"
	return (proc.stdout or "").strip() or "ok"


def probe_proxy_status() -> str:
	try:
		resp = HTTP.get("https://www.youtube.com/generate_204", timeout=10)
		return f"ok ({resp.status_code})"
	except Exception as exc:
		return f"error: {exc}"


def probe_google_calendar_status() -> str:
	required = (
		"GOOGLE_CLIENT_ID",
		"GOOGLE_CLIENT_SECRET",
		"GOOGLE_REFRESH_TOKEN",
	)
	missing = [name for name in required if not os.environ.get(name, "").strip()]
	if missing:
		return "missing " + ", ".join(missing)
	return f"configured (calendar={google_calendar_id()}, tz={google_calendar_timezone()})"


def build_status_text() -> str:
	lines = [
		"Status:",
		f"uptime: {format_uptime(time.time() - STARTED_AT)}",
		f"pending choices: {len(PENDING_BY_CHAT)}",
		f"proxy: {probe_proxy_status()}",
		f"yt-dlp: {probe_ytdlp_status()}",
		f"google calendar: {probe_google_calendar_status()}",
	]
	return "\n".join(lines)


def social_media_fallback_name(url: str) -> str:
	try:
		parsed = urlparse(url.strip())
	except Exception:
		return "social_media"
	host = (parsed.netloc or "").lower().replace("www.", "")
	parts = [part for part in (parsed.path or "").split("/") if part]
	host_name = host.split(".")[0] or "social"
	if len(parts) >= 2:
		return sanitize_name(f"{host_name}_{parts[0]}_{parts[1]}")
	if parts:
		return sanitize_name(f"{host_name}_{parts[0]}")
	return sanitize_name(f"{host_name}_media")


def social_media_base_name(url: str) -> str:
	yt_bin = ytdlp_path()
	cmd = [
		yt_bin,
		"--dump-single-json",
		"--proxy",
		PROXY_URL,
		"--no-playlist",
		"--socket-timeout",
		"30",
		url,
	]
	try:
		proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
		if proc.returncode != 0:
			return social_media_fallback_name(url)
		payload = json.loads(proc.stdout)
	except Exception:
		return social_media_fallback_name(url)

	title = str(payload.get("title") or "").strip()
	uploader = str(payload.get("uploader") or payload.get("channel") or "").strip()
	if title and uploader:
		base_name = f"{uploader} - {title}"
	elif title:
		base_name = title
	elif uploader:
		base_name = uploader
	else:
		base_name = social_media_fallback_name(url)
	return sanitize_name(base_name)


def rename_social_media_files(files: List[pathlib.Path], base_name: str) -> List[pathlib.Path]:
	if not files:
		return files
	renamed = []
	multiple = len(files) > 1
	for index, path in enumerate(sorted(files), start=1):
		suffix = path.suffix.lower()
		stem = f"{base_name}_{index:02d}" if multiple else base_name
		target = path.with_name(f"{stem}{suffix}")
		counter = 2
		while target.exists() and target != path:
			target = path.with_name(f"{stem}_{counter:02d}{suffix}")
			counter += 1
		if target != path:
			path = path.rename(target)
		renamed.append(path)
	return renamed


def download_audio(video_id: str, title: str, artist: str, out_dir: pathlib.Path) -> pathlib.Path:
	out_dir.mkdir(parents=True, exist_ok=True)
	base_name = sanitize_name(f"{artist} - {title}")
	out_template = str(out_dir / f"{base_name}.%(ext)s")
	yt_bin = ytdlp_path()
	last_error = "yt-dlp failed without error output"

	for base_url in (YTM_URL.format(vid=video_id), YT_URL.format(vid=video_id)):
		for client in YOUTUBE_CLIENTS:
			cmd = [
				yt_bin,
				"-f",
				"ba[ext=m4a]/bestaudio[ext=m4a]/bestaudio",
				"--proxy",
				PROXY_URL,
				"--no-playlist",
				"--force-overwrites",
				"--retries",
				"5",
				"--fragment-retries",
				"5",
				"--socket-timeout",
				"30",
				"--extractor-args",
				f"youtube:player_client={client}",
				"-o",
				out_template,
				base_url,
			]
			proc = subprocess.run(cmd, capture_output=True, text=True)
			if proc.returncode == 0:
				files = sorted(out_dir.glob(f"{base_name}.*"), key=lambda path: path.stat().st_mtime, reverse=True)
				if files:
					return files[0]
				raise RuntimeError("yt-dlp reported success but no file was created.")
			last_error = (proc.stderr or proc.stdout or "").strip()[-500:]

	raise RuntimeError(f"Download failed: {last_error}")


def download_social_media(url: str, out_dir: pathlib.Path) -> List[pathlib.Path]:
	out_dir.mkdir(parents=True, exist_ok=True)
	out_template = str(out_dir / "social_%(autonumber)03d.%(ext)s")
	yt_bin = ytdlp_path()
	cmd = [
		yt_bin,
		"--merge-output-format",
		"mp4",
		"--proxy",
		PROXY_URL,
		"--no-playlist",
		"--force-overwrites",
		"--retries",
		"5",
		"--fragment-retries",
		"5",
		"--socket-timeout",
		"30",
		"-o",
		out_template,
		url,
	]
	proc = subprocess.run(cmd, capture_output=True, text=True)
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout or "").strip()[-700:]
		raise RuntimeError(f"yt-dlp failed: {detail}")
	files = sorted(
		[
			path for path in out_dir.iterdir()
			if path.is_file()
			and path.name.startswith("social_")
			and path.suffix.lower() not in {".part", ".ytdl", ".json", ".description", ".vtt", ".srt", ".ass"}
		]
	)
	if not files:
		raise RuntimeError("yt-dlp reported success but no media file was created.")
	return rename_social_media_files(files, social_media_base_name(url))


def social_media_kind(path: pathlib.Path) -> str:
	suffix = path.suffix.lower()
	if suffix in {".jpg", ".jpeg", ".png"}:
		return "photo"
	if suffix in {".mp4", ".mov", ".m4v", ".webm"}:
		return "video"
	return "document"


def send_social_media(chat_id: int, path: pathlib.Path, caption: Optional[str] = None) -> None:
	kind = social_media_kind(path)
	if kind == "photo":
		send_photo(chat_id, path, caption)
		return
	if kind == "video":
		send_video(chat_id, path, caption)
		return
	send_document(chat_id, path, caption)


def can_send_as_media_group(paths: List[pathlib.Path]) -> bool:
	return len(paths) > 1 and all(social_media_kind(path) in {"photo", "video"} for path in paths)


def format_candidates(candidates: List[Dict], limit: int = 5) -> str:
	lines = ["Tap a result button below, or reply with a number:"]
	for index, candidate in enumerate(candidates[:limit], start=1):
		title = candidate.get("title") or "Unknown title"
		artist = candidate_artist_text(candidate) or "Unknown artist"
		score = candidate.get("score", 0.0)
		source = candidate.get("source") or "unknown"
		duration = duration_s(candidate.get("duration_seconds"))
		duration_text = f"{duration}s" if duration else "unknown duration"
		lines.append(f"{index}. {artist} - {title} | {duration_text} | {source} | score={score:.2f}")
	return "\n".join(lines)


def candidate_buttons(candidates: List[Dict], limit: int = 5) -> Dict:
	keyboard = []
	for index, candidate in enumerate(candidates[:limit], start=1):
		title = candidate.get("title") or "Unknown title"
		artist = candidate_artist_text(candidate) or "Unknown artist"
		button_text = f"{index}. {artist[:20]} - {title[:24]}"
		keyboard.append([{"text": button_text, "callback_data": f"pick:{index}"}])
	return {"inline_keyboard": keyboard}


def cleanup_old_pending() -> None:
	now = time.time()
	for chat_id in list(PENDING_BY_CHAT.keys()):
		if now - PENDING_BY_CHAT[chat_id].created_at > 900:
			del PENDING_BY_CHAT[chat_id]


def handle_search(chat_id: int, query: str) -> None:
	send_chat_action(chat_id, "typing")
	_, confidence, ranked = find_best(query)
	if not ranked:
		send_message(chat_id, f"No candidates found for: {query}")
		return

	PENDING_BY_CHAT[chat_id] = PendingChoice(query=query, candidates=ranked[:5], created_at=time.time())
	prefix = f"Best automatic match score: {confidence:.2f}\n" if confidence > 0 else ""
	send_message(chat_id, prefix + format_candidates(ranked, limit=5), reply_markup=candidate_buttons(ranked, limit=5))


def handle_choice(chat_id: int, text: str) -> None:
	pending = PENDING_BY_CHAT.get(chat_id)
	if not pending:
		send_message(chat_id, "Send a search query first.")
		return

	try:
		index = int(text.strip())
	except ValueError:
		send_message(chat_id, "Send a number from the candidate list.")
		return

	if not (1 <= index <= len(pending.candidates)):
		send_message(chat_id, "Choice out of range.")
		return

	selected = pending.candidates[index - 1]
	video_id = selected.get("videoId")
	if not video_id:
		send_message(chat_id, "That candidate has no video id.")
		return

	title = selected.get("title") or pending.query
	artist = candidate_artist_text(selected) or "Unknown artist"
	send_message(chat_id, f"Downloading: {artist} - {title}")
	send_chat_action(chat_id, "upload_document")

	with tempfile.TemporaryDirectory(prefix="tgmusic-") as tmpdir:
		tmp_path = pathlib.Path(tmpdir)
		output_file = download_audio(video_id, title, artist, tmp_path)
		cover_bytes = yt_thumbnail_bytes(video_id)
		tag_file(output_file, title, artist, cover_bytes)
		send_audio(chat_id, output_file, title, artist, cover_bytes)

	del PENDING_BY_CHAT[chat_id]


def handle_callback_query(callback_query: Dict) -> None:
	callback_query_id = callback_query.get("id")
	from_user = callback_query.get("from") or {}
	user_id = from_user.get("id")
	if user_id != ALLOWED_USER_ID:
		if callback_query_id:
			answer_callback_query(callback_query_id, "Not allowed.", show_alert=True)
		return

	message = callback_query.get("message") or {}
	chat = message.get("chat") or {}
	chat_id = chat.get("id")
	message_id = message.get("message_id")
	data = (callback_query.get("data") or "").strip()

	if not callback_query_id or not isinstance(chat_id, int):
		return
	if not data.startswith("pick:"):
		answer_callback_query(callback_query_id, "Unknown action.", show_alert=False)
		return

	pending = PENDING_BY_CHAT.get(chat_id)
	if not pending:
		answer_callback_query(callback_query_id, "This result list expired. Search again.", show_alert=True)
		return

	try:
		index = int(data.split(":", 1)[1])
	except Exception:
		answer_callback_query(callback_query_id, "Invalid choice.", show_alert=True)
		return

	if not (1 <= index <= len(pending.candidates)):
		answer_callback_query(callback_query_id, "Choice out of range.", show_alert=True)
		return

	answer_callback_query(callback_query_id, f"Selected result {index}")
	if isinstance(message_id, int):
		try:
			edit_message_reply_markup(chat_id, message_id, {"inline_keyboard": []})
		except Exception:
			pass
	handle_choice(chat_id, str(index))


def handle_direct_link(chat_id: int, url: str) -> None:
	video_id = extract_youtube_video_id(url)
	if not video_id:
		send_message(chat_id, "Could not extract a YouTube video id from that link.")
		return

	title, artist = probe_video_metadata(url, video_id)
	send_message(chat_id, f"Downloading from link: {artist} - {title}")
	send_chat_action(chat_id, "upload_document")

	with tempfile.TemporaryDirectory(prefix="tgmusic-") as tmpdir:
		tmp_path = pathlib.Path(tmpdir)
		output_file = download_audio(video_id, title, artist, tmp_path)
		cover_bytes = yt_thumbnail_bytes(video_id)
		tag_file(output_file, title, artist, cover_bytes)
		send_audio(chat_id, output_file, title, artist, cover_bytes)


def handle_social_video_link(chat_id: int, url: str) -> None:
	send_message(chat_id, "Downloading media...")
	send_chat_action(chat_id, "upload_document")

	with tempfile.TemporaryDirectory(prefix="tgsocial-") as tmpdir:
		tmp_path = pathlib.Path(tmpdir)
		output_files = download_social_media(url, tmp_path)
		try:
			if can_send_as_media_group(output_files):
				send_media_group(chat_id, output_files, caption=url)
			else:
				for index, output_file in enumerate(output_files):
					caption = url if index == 0 and len(output_files) > 1 else None
					send_social_media(chat_id, output_file, caption)
		finally:
			for output_file in output_files:
				try:
					output_file.unlink()
				except Exception:
					pass


def extract_message(update: Dict) -> Optional[Dict]:
	message = update.get("message")
	if not isinstance(message, dict):
		return None
	return message


def handle_message(message: Dict) -> None:
	from_user = message.get("from") or {}
	user_id = from_user.get("id")
	if user_id != ALLOWED_USER_ID:
		return

	chat = message.get("chat") or {}
	chat_id = chat.get("id")
	if not isinstance(chat_id, int):
		return

	text = (message.get("text") or "").strip()
	if not text:
		return

	if text == "/start":
		send_message(
			chat_id,
			"Send a song search query or a supported media link.\n"
			"You can also send a schedule message starting with `🗓 Расписание`, and the bot will sync shifts to Google Calendar.",
		)
		return

	if text == "/help":
		send_message(
			chat_id,
			"Usage:\n"
			"1. Send a song search query\n"
			"2. Tap one of the result buttons, or reply with 1-5\n"
			"3. Send a YouTube / YouTube Music link for audio\n"
			"4. Send a TikTok or Instagram link for media\n"
			"5. Send a schedule message starting with `🗓 Расписание` to sync shifts to Google Calendar\n"
			"6. Send /status to check bot health\n"
			"Only your user id is allowed.",
		)
		return

	if text == "/status":
		send_chat_action(chat_id, "typing")
		send_message(chat_id, build_status_text())
		return

	if is_schedule_message(text):
		try:
			handle_schedule_message(chat_id, text)
		except Exception as exc:
			send_message(
				chat_id,
				"Не удалось синхронизировать расписание в Google Calendar.\n"
				f"Ошибка: {exc}\n\n"
				"Проверь формат сообщения и переменные GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN.",
			)
		return

	if re.fullmatch(r"\d+", text):
		handle_choice(chat_id, text)
		return

	if is_youtube_url(text):
		handle_direct_link(chat_id, text)
		return

	if is_social_video_url(text):
		handle_social_video_link(chat_id, text)
		return

	handle_search(chat_id, text)


def run_bot() -> int:
	load_dotenv()
	configure_proxy()
	offset = None
	print(f"Bot started. Allowed user id: {ALLOWED_USER_ID}")
	while True:
		try:
			cleanup_old_pending()
			updates = get_updates(offset)
			for update in updates:
				offset = update["update_id"] + 1
				callback_query = update.get("callback_query")
				if isinstance(callback_query, dict):
					handle_callback_query(callback_query)
					continue
				message = extract_message(update)
				if message is None:
					continue
				handle_message(message)
		except KeyboardInterrupt:
			print("Bot stopped.")
			return 0
		except Exception as exc:
			print(f"Bot loop error: {exc}", file=sys.stderr)
			time.sleep(3)


if __name__ == "__main__":
	sys.exit(run_bot())
