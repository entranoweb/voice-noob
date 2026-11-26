"""Cutting rules and labels, on word lists made up for the purpose. No audio."""

from pathlib import Path

from synthiq_caller.clipbank.build import MANIFEST_COLUMNS, build
from synthiq_caller.clipbank.from_call import scenario_from_call
from synthiq_caller.clipbank.segments import label, plan_clips
from synthiq_caller.clipbank.values import clip_value, spoken_characters
from synthiq_caller.scenario import parse_scenario


def words(*turns: tuple[str, float, str]) -> list[dict[str, object]]:
    """``(speaker, start, text)`` turns as words 0.3 s long, 0.1 s apart."""
    out: list[dict[str, object]] = []
    for speaker, start, text in turns:
        at = start
        for word in text.split():
            out.append({"text": word, "start": at, "end": at + 0.3, "speaker_id": speaker})
            at += 0.4
    return out


def test_a_hesitant_answer_is_one_clip_and_keeps_its_silence() -> None:
    transcript = words(
        ("agent", 0.0, "Could I take your postcode, please?"),
        ("caller", 4.0, "B nine"),
        ("caller", 6.5, "zero RS."),  # 1.8 s after "nine": one answer, said in two breaths
        ("agent", 9.0, "And will you be using health insurance?"),
    )
    plans = plan_clips(transcript)
    assert len(plans) == 1
    assert plans[0].text == "B nine zero RS."
    assert plans[0].review == ["split_merged"]
    assert plans[0].start == 4.0
    assert plans[0].end > 6.5  # the pause is inside the clip, not cut out


def test_answers_to_different_questions_are_not_merged() -> None:
    transcript = words(
        ("agent", 0.0, "Could I take your full name, please?"),
        ("caller", 3.0, "Elliot Allen."),
        ("agent", 4.2, "Thanks. And have you been to see us before?"),
        ("caller", 8.0, "No."),
    )
    plans = plan_clips(transcript)
    assert [p.category for p in plans] == ["name", "been_before"]
    assert all("split_merged" not in p.review for p in plans)


def test_a_long_pause_is_two_clips() -> None:
    transcript = words(
        ("agent", 0.0, "When would suit you best?"),
        ("caller", 3.0, "Wednesday."),
        ("caller", 7.0, "Morning."),  # 3.7 s later: past the merge limit
    )
    assert len(plan_clips(transcript)) == 2


def test_a_clip_that_overlaps_the_agent_is_flagged_not_dropped() -> None:
    transcript = [
        {"text": "Could", "start": 0.0, "end": 0.3, "speaker_id": "agent"},
        {"text": "Yes.", "start": 0.35, "end": 0.6, "speaker_id": "caller"},
        {"text": "I", "start": 0.62, "end": 0.7, "speaker_id": "agent"},
    ]
    plans = plan_clips(transcript)
    assert len(plans) == 1
    assert "overlaps_agent" in plans[0].review


def test_a_stray_noise_is_flagged_too_short() -> None:
    transcript = [
        {"text": "Hello?", "start": 0.0, "end": 0.5, "speaker_id": "agent"},
        {"text": "B-", "start": 3.0, "end": 3.05, "speaker_id": "caller"},
    ]
    assert plan_clips(transcript)[0].review == ["too_short"]


def test_a_yes_to_a_read_back_is_a_confirmation_not_a_date_of_birth() -> None:
    read_back = "I have the name Elliot Allen, with the date of birth 6 July 1987. Is that right?"
    assert label(read_back, "Correct.") == "confirm_details"
    assert label(read_back, "Yes.") == "confirm_details"
    assert label(read_back, "Uh, no. It's fifth July.") == "correction"
    assert label("Could I take your date of birth, please?", "5th July 1987.") == "dob"


def test_each_kind_of_confirmation_gets_its_own_label() -> None:
    assert label("the self-pay fee is £250. Is that all right?", "That's fine.") == "confirm_fee"
    practitioner = "That would be with Jordan Example. Is that all right?"
    assert label(practitioner, "Yes, please.") == "confirm_practitioner"
    booking = "That's a consultation on Monday at midday. Is that right?"
    assert label(booking, "Yes, please.") == "confirm_booking"
    assert (
        label("on Friday at one. Is that the one you'd like to move?", "Yes.") == "confirm_choice"
    )


def test_questions_are_labelled_by_what_they_ask() -> None:
    body = "Could you tell me which part of the body your scan referral is for?"
    assert label(body, "Knee.") == "body_part"
    assert label("And finally, your post code, please.", "S one five O A D.") == "postcode"
    assert label("Have a lovely day. Bye.", "Bye. Thank you.") == "goodbye"
    assert label("Something unrelated.", "Hmm.") == "other"


def test_spoken_letters_and_digits_spell_the_value() -> None:
    assert spoken_characters("It's A five six T B.") == "A56TB"
    assert spoken_characters("B forty-five eight HT.") == "B458HT"
    assert spoken_characters("E-one-two-X-Y.") == "E12XY"
    assert spoken_characters("S one five O A D.") == "S15OAD"
    assert spoken_characters("M two three zed Y") == "M23ZY"
    assert clip_value("phone", "Zero seven eight double two nine") == "078229"
    assert clip_value("dob", "Fifth July nineteen eighty-seven.") == "1987-07-05"
    assert clip_value("name", "It's Elliot Allen.") == "elliot allen"
    assert clip_value("reason", "Having knee pain.") == ""


def test_build_writes_a_manifest_without_needing_audio(tmp_path: Path) -> None:
    import json

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    transcript = words(
        ("agent", 0.0, "How can I help you today?"),
        ("caller", 3.0, "Can you book an appointment for me?"),
        ("agent", 6.0, "Could I take your date of birth, please?"),
        ("caller", 10.0, "Fifth July nineteen eighty-seven."),
    )
    for word in transcript:
        word["type"] = "word"
    (recordings / "call-1.stt.json").write_text(json.dumps({"words": transcript}), encoding="utf-8")
    (recordings / "call-1.mp3").write_bytes(b"")  # present, never opened

    out = tmp_path / "bank"
    summary = build(recordings, out, write_audio=False)

    assert summary["calls"] == 1 and summary["clips"] == 2
    header, first, second = (out / "manifest.csv").read_text(encoding="utf-8").splitlines()
    assert header.split(",") == list(MANIFEST_COLUMNS)
    assert first.startswith("call-1_01_opener,call-1,opener,")
    assert "1987-07-05" in second


def test_a_recorded_call_becomes_a_scenario_that_parses(tmp_path: Path) -> None:
    (tmp_path / "manifest.csv").write_text(
        "clip,call,category,start,end,secs,said,intended,value,question,review,checked_by_human\n"
        "c_01_opener,c,opener,0,1,1,Book please,Book please,,How can I help?,,\n"
        "c_02_name,c,name,0,1,1,Elliot Allen.,Elliot Allen.,elliot allen,full name?,,\n"
        "c_03_dob,c,dob,0,1,1,5th July 1987.,5th July 1987.,1987-07-05,date of birth?,,\n"
        "c_04_confirm_details,c,confirm_details,0,1,1,Yes.,Yes.,,Is that right?,,\n"
        "c_05_other,c,other,0,1,1,Hmm.,Hmm.,,,overlaps_agent,\n",
        encoding="utf-8",
    )
    data = scenario_from_call(tmp_path, "c")
    scenario = parse_scenario(data)

    assert scenario.mutates == "unknown"  # a person must say what it changes
    assert scenario.facts == {"name": "elliot allen", "date_of_birth": "1987-07-05"}
    # The read-back confirmation is matched before the question that asks for the date.
    patterns = [step.when.pattern for step in scenario.steps]
    assert patterns.index("i have the name|that's the name") < patterns.index("date of birth")
    assert data["notes"]["clips_not_placed"] == ["c_05_other (other; overlaps_agent)"]
