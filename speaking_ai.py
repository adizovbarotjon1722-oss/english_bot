"""
speaking_ai.py — Ovozli speaking tahlili.
OPENAI_API_KEY yoki GROQ_API_KEY bo'lsa — real AI.
Aks holda — strukturalangan heuristik feedback (o'qituvchi yakuniy baholaydi).
"""

from __future__ import annotations

import os
import logging
from typing import Optional, Tuple

logger = logging.getLogger("repetitor_bot.speaking")


def analyze_speaking(
    prompt: str,
    duration_sec: int,
    min_seconds: int = 15,
    max_seconds: int = 120,
    language: str = "en",
) -> Tuple[int, str]:
    """
    Qaytaradi: (score 0-100, feedback matn).
    API kalit bo'lmasa ham ishlaydi.
    """
    score = 50
    tips = []

    if duration_sec < min_seconds:
        score -= 25
        tips.append(f"Juda qisqa ({duration_sec}s). Kamida {min_seconds} soniya gapirishga harakat qiling.")
    elif duration_sec > max_seconds:
        score -= 5
        tips.append("Biroz uzun bo'ldi — asosiy fikrlarni qisqaroq ifodalashni mashq qiling.")
    else:
        score += 15
        tips.append(f"Davomiylik yaxshi ({duration_sec}s).")

    # Heuristic structure advice by level of prompt
    if language == "en":
        tips.append("Inglizcha tuzilma: boshlang'ich gap → 2–3 misol → xulosa.")
        tips.append("Present Simple/Continuous ni vazifaga mos ishlating.")
        tips.append("Sekin, aniq talaffuz — tushunarliligi eng muhim.")
    else:
        tips.append("Ruscha: to'liq gaplar, to'g'ri urg'u va intonatsiya.")
        tips.append("Oddiy sxema: kirish → asosiy qism → xulosa.")

    # Optional real AI (Whisper + chat) if key present
    api_feedback = _try_llm_feedback(prompt, duration_sec, language)
    if api_feedback:
        score = min(100, max(score, 60))
        tips.append("AI tahlil: " + api_feedback)

    score = max(0, min(100, score))
    header = {
        range(80, 101): "Ajoyib urinish! 🌟",
        range(60, 80): "Yaxshi, davom eting! 💪",
        range(40, 60): "O'rtacha — mashq qilsangiz yaxshilanadi.",
        range(0, 40): "Qayta urinib ko'ring — sekinroq va aniqroq.",
    }
    title = "Speaking tahlil"
    for r, t in header.items():
        if score in r:
            title = t
            break

    feedback = title + "\n\n" + "\n".join(f"• {t}" for t in tips)
    feedback += "\n\n📌 O'qituvchi ham tinglab, qo'shimcha izoh beradi."
    return score, feedback


def _try_llm_feedback(prompt: str, duration: int, language: str) -> Optional[str]:
    """Ixtiyoriy: OpenAI / Groq. Kalit bo'lmasa None."""
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not openai_key and not groq_key:
        return None
    try:
        import urllib.request
        import json as _json

        system = (
            "You are a friendly language teacher. Give 2-3 short practical tips "
            "for a speaking task. No transcription available; judge by task prompt "
            f"and duration={duration}s only. Reply in Uzbek, max 300 chars."
        )
        user = f"Task prompt: {prompt}\nLanguage: {language}\nDuration: {duration}s"

        if groq_key:
            url = "https://api.groq.com/openai/v1/chat/completions"
            key = groq_key
            model = "llama-3.1-8b-instant"
        else:
            url = "https://api.openai.com/v1/chat/completions"
            key = openai_key
            model = "gpt-4o-mini"

        body = _json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 200,
            "temperature": 0.5,
        }).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning("LLM speaking feedback failed: %s", e)
        return None



def transcribe_voice_file(file_path: str, language: str = "en") -> Optional[str]:
    """OpenAI Whisper API or Groq whisper. file_path — local path to ogg/mp3."""
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not openai_key and not groq_key:
        return None
    try:
        import urllib.request
        import json as _json

        # multipart form is awkward with urllib; use simple approach if openai package missing
        try:
            if groq_key:
                from urllib import request as urlreq
                # Groq audio transcriptions
                boundary = "----FormBoundary7MA4YWxkTrZu0gW"
                with open(file_path, "rb") as f:
                    audio = f.read()
                body = (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="file"; filename="voice.ogg"\r\n'
                    f"Content-Type: application/octet-stream\r\n\r\n"
                ).encode() + audio + (
                    f"\r\n--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="model"\r\n\r\n'
                    f"whisper-large-v3\r\n"
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="language"\r\n\r\n'
                    f"{language}\r\n"
                    f"--{boundary}--\r\n"
                ).encode()
                req = urlreq.Request(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    data=body,
                    headers={
                        "Authorization": f"Bearer {groq_key}",
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                    method="POST",
                )
                with urlreq.urlopen(req, timeout=60) as resp:
                    data = _json.loads(resp.read().decode())
                return (data.get("text") or "").strip() or None
            if openai_key:
                # Prefer openai SDK if installed
                try:
                    from openai import OpenAI
                    client = OpenAI(api_key=openai_key)
                    with open(file_path, "rb") as f:
                        tr = client.audio.transcriptions.create(
                            model="whisper-1", file=f, language=language
                        )
                    return (tr.text or "").strip() or None
                except ImportError:
                    logger.info("openai package yo'q — Whisper o'tkazib yuborildi")
                    return None
        except Exception as e:
            logger.warning("Whisper fail: %s", e)
            return None
    except Exception as e:
        logger.warning("transcribe_voice_file: %s", e)
        return None


def analyze_with_transcript(
    prompt: str,
    duration_sec: int,
    transcript: Optional[str],
    min_seconds: int = 15,
    max_seconds: int = 120,
    language: str = "en",
) -> tuple:
    score, feedback = analyze_speaking(prompt, duration_sec, min_seconds, max_seconds, language)
    if transcript:
        words = len(transcript.split())
        score = min(100, score + min(20, words // 5))
        feedback += f"\n\n📝 <b>Whisper matn:</b>\n{transcript[:500]}"
        if words < 8:
            feedback += "\n• Juda kam so'z — ko'proq gapirishga harakat qiling."
            score = max(0, score - 10)
        else:
            feedback += f"\n• Taxminan {words} so'z — yaxshi hajm."
    return max(0, min(100, score)), feedback
