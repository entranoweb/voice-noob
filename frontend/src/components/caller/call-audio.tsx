"use client";

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchCallAudio, type CallAudioRef } from "@/lib/api/caller-runs";

export type MarkerKind = "caller" | "agent" | "tool";

export interface AudioMarker {
  t: number;
  kind: MarkerKind;
  label: string;
}

export interface CallAudioHandle {
  /** Move both tracks to `seconds` and start playing. */
  seek: (seconds: number) => void;
}

const TRACK_LABEL: Record<string, string> = {
  caller: "Caller (what the rig played)",
  agent: "Agent (what the agent said)",
};

const KIND_LABEL: Record<MarkerKind, string> = {
  caller: "caller clip played",
  agent: "agent spoke",
  tool: "tool call",
};

const KIND_STYLE: Record<MarkerKind, string> = {
  caller: "bg-blue-600",
  agent: "bg-emerald-600",
  tool: "bg-amber-600",
};

type Loaded =
  | { state: "loading" }
  | { state: "ready"; url: string }
  | { state: "failed"; message: string };

interface Props {
  audio: CallAudioRef[];
  markers: AudioMarker[];
  /** The call had recordings and retention has deleted them. */
  expired?: boolean;
}

/**
 * Two tracks (what the rig played, what the agent said) above the timeline.
 *
 * The browser cannot send the sign-in token with an audio element's own request,
 * so each file is fetched with the token and played from a local object URL.
 * Without audio, this says so: it never draws a player that cannot play.
 */
export const CallAudio = forwardRef<CallAudioHandle, Props>(function CallAudio(
  { audio, markers, expired = false },
  ref
) {
  const elements = useRef<Record<string, HTMLAudioElement | null>>({});
  const [loaded, setLoaded] = useState<Record<string, Loaded>>({});
  const [length, setLength] = useState(0);
  const key = audio.map((a) => a.url).join("|");

  useEffect(() => {
    let cancelled = false;
    const made: string[] = [];
    setLoaded(Object.fromEntries(audio.map((a) => [a.track, { state: "loading" }])));
    for (const item of audio) {
      fetchCallAudio(item.url)
        .then((blob) => {
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          made.push(url);
          setLoaded((current) => ({ ...current, [item.track]: { state: "ready", url } }));
        })
        .catch(() => {
          if (cancelled) return;
          setLoaded((current) => ({
            ...current,
            [item.track]: {
              state: "failed",
              message: "This recording could not be loaded. It may have been deleted.",
            },
          }));
        });
    }
    return () => {
      cancelled = true;
      for (const url of made) URL.revokeObjectURL(url);
    };
    // `key` stands for the list of files; `audio` itself is a new array every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const seek = useCallback((seconds: number) => {
    const all = Object.values(elements.current).filter((el): el is HTMLAudioElement => el !== null);
    for (const el of all) {
      el.currentTime = Math.max(0, seconds);
    }
    // One side plays at a time: the first that is there. The other follows by position.
    const first = all[0];
    if (first) void Promise.resolve(first.play()).catch(() => undefined);
  }, []);

  useImperativeHandle(ref, () => ({ seek }), [seek]);

  if (audio.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Audio</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground" role="status">
            {expired
              ? "Recording expired. The audio of this call was deleted when its retention period ended; the timeline below is kept."
              : "No audio is stored for this call. It was not recorded or was not uploaded."}
          </p>
        </CardContent>
      </Card>
    );
  }

  const total = Math.max(length, ...audio.map((a) => a.duration_s), 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Audio</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-3">
          {audio.map((item) => {
            const status = loaded[item.track] ?? { state: "loading" };
            return (
              <div key={item.track} className="space-y-1">
                <p className="text-sm font-medium" id={`audio-${item.track}`}>
                  {TRACK_LABEL[item.track] ?? item.track}
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {item.duration_s.toFixed(1)}s
                  </span>
                </p>
                {status.state === "ready" ? (
                  <audio
                    ref={(el) => {
                      elements.current[item.track] = el;
                    }}
                    controls
                    preload="metadata"
                    src={status.url}
                    aria-labelledby={`audio-${item.track}`}
                    className="w-full"
                    onLoadedMetadata={(event) =>
                      setLength((current) => Math.max(current, event.currentTarget.duration || 0))
                    }
                  />
                ) : status.state === "failed" ? (
                  <p className="text-sm text-red-700 dark:text-red-400" role="alert">
                    {status.message}
                  </p>
                ) : (
                  <p className="text-sm text-muted-foreground">Loading recording...</p>
                )}
              </div>
            );
          })}
        </div>

        {markers.length > 0 && total > 0 && (
          <div>
            <div
              role="group"
              aria-label="Markers on the recording"
              className="relative h-6 rounded bg-muted"
            >
              {markers.map((marker, index) => (
                <button
                  key={`${marker.kind}-${marker.t}-${index}`}
                  type="button"
                  title={`${KIND_LABEL[marker.kind]} at ${marker.t.toFixed(1)}s`}
                  aria-label={`${KIND_LABEL[marker.kind]} at ${marker.t.toFixed(1)} seconds`}
                  data-marker={marker.kind}
                  onClick={() => seek(marker.t)}
                  className={`absolute top-0 h-6 w-1.5 -translate-x-1/2 rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${KIND_STYLE[marker.kind]}`}
                  style={{ left: `${Math.min(100, (marker.t / total) * 100)}%` }}
                />
              ))}
            </div>
            <ul className="mt-2 flex flex-wrap gap-4 text-xs text-muted-foreground">
              {(Object.keys(KIND_LABEL) as MarkerKind[]).map((kind) => (
                <li key={kind} className="flex items-center gap-1">
                  <span className={`inline-block h-3 w-1.5 rounded-sm ${KIND_STYLE[kind]}`} />
                  {KIND_LABEL[kind]}
                </li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-muted-foreground">
              Times are from when the call connected, and line up with the recording to within a
              moment, not exactly.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
});
