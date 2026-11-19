"""T2-FIX2c (spec v3 §8 / build step 7, vehicle part): plate-text recovery BEFORE a new presence is published.
While a new vehicle track is still unconfirmed (nothing emitted for it yet), an OCR read >= ocr_emit_min_confidence whose
text equals the CONFIRMED plate of a lost vehicle presence (same camera, same use case, inside its end grace) rejoins the
track to that presence, exactly like the existing body/position reassociation: same presence ID, presence_recovered
emitted on the next frame. Not done when any active (not lost) vehicle presence holds the same text (conflict), or when
two lost presences hold it. Default OFF: SENTINEL_PLATE_RECOVERY=1 enables it."""
import sys
p = sys.argv[1]; s = open(p).read()
def rep(old, new):
    global s
    assert s.count(old) == 1, old[:70]; s = s.replace(old, new)
rep("""            elif state["lost"]:
                recovered_from = track_id
""", """            elif state["lost"]:
                recovered_from = state.pop("t2_plate_recovered_from", track_id)
""")
rep("""            key = ("vehicle", str(plate.parent_id))
            state = self.tracks.get(key)
            if state is None or not state.get("confirmed"):
                continue
""", """            key = ("vehicle", str(plate.parent_id))
            state = self.tracks.get(key)
            if (state is not None and not state.get("confirmed")
                    and os.getenv("SENTINEL_PLATE_RECOVERY", "0") == "1"):
                self._t2_plate_recover(state, key, plate, now)
                state = self.tracks.get(key)
            if state is None or not state.get("confirmed"):
                continue
""")
rep("""    def _plates(self, packet, now, frame_id):
""", """    def _t2_plate_recover(self, fresh, key, plate, now):
        \"\"\"T2-FIX2c: rejoin an unconfirmed new vehicle track to a lost presence with the same confirmed plate.\"\"\"
        text = re.sub(r"[^A-Z0-9? -]", "", str(plate.metadata.get("ocr_text") or "").upper()).strip()[:32]
        if not text or "?" in text:
            return
        confidence = max(0.0, min(1.0, float(plate.metadata.get("ocr_confidence", plate.confidence))))
        if confidence < float(self.policy.section("anpr")["ocr_emit_min_confidence"]):
            return
        owners = [state for state in self.presences.values()
                  if state["kind"] == "vehicle" and state is not fresh and state.get("confirmed")
                  and any(value[0] == text for value in state["plates"].values())]
        if any(not state["lost"] for state in owners):
            return  # same plate visible on an active vehicle: conflict, never merge
        lost = [state for state in owners if now - state["last_seen"] <= self._presence_timeouts(state)[1]]
        if len(lost) != 1:
            return
        old = lost[0]
        self.tracks.pop(old["track_key"], None)
        self.tracks.pop(key, None)
        self.presences.pop(fresh["id"], None)
        self._retire_gait_state(fresh["id"])
        old["t2_plate_recovered_from"] = old["track_id"]
        old["track_key"] = key
        old["track_id"] = key[1]
        self.tracks[key] = old
        if self.telemetry is not None:
            self.telemetry.count("t2_plate_recoveries")

    def _plates(self, packet, now, frame_id):
""")
open(p, 'w').write(s); print('ok')
