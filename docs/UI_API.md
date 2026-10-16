# Local UI contract (v0.2)

The local HTTP server binds only to 127.0.0.1 on an ephemeral port. No framework, CDN, analytics, or hosted API. `web/index.html`, `web/styles.css`, `web/app.js` are served locally. Browser opens `/#token=RANDOM`; JS stores token in sessionStorage and removes fragment with history.replaceState. Every `/api/*` request needs `X-Local-Token`. Use fetch+Blob for downloads; never put token in query strings. Render source/transcript/logs with textContent, never innerHTML.

- GET `/api/state`: `{name:"Local Transcriber",version:"0.2.0",setup:{ready:true,message:"Ready"},active_id:null,jobs:[summary,...]}`.
- Job summary: `{id,title,source,language,device,status,created_at,progress,completed_parts,total_parts,error}`. status is queued/running/stopping/stopped/completed/failed. progress is 0..100. One active job maximum. newest jobs first.
- GET `/api/jobs/ID`: summary plus `{logs:[string,...],preview:string,output_dir:string|null,files:[{format:"txt",name:"transcript.txt"},{format:"timestamps",name:"transcript.timestamps.txt"},{format:"srt",name:"transcript.srt"},{format:"json",name:"transcript.json"}]}`. Files can contain partial text while running; status says completed only when finished. preview is plain text, bounded tail; downloads contain the whole current export.
- POST `/api/jobs` JSON `{source:string,language:"auto"|"en"|"ru",device:"auto"|"cuda"|"cpu"}` -> `{job:summary}` (202). Same source/language reuse backend checkpoints; label button Start transcription / Resume for stopped jobs, not restart from scratch. Server decides progress and availability.
- POST `/api/stop` JSON `{}` -> `{ok:true}` (202); stop current active job. Poll state until stopped/completed. Do not promise instant stop; finished blocks are preserved.
- POST `/api/select-file` JSON `{}` -> `{path:string}`. Opens native file picker; cancellation gives empty string. Frontend uses source URL/path text input and a Choose file button; no browser upload required, no file sent to cloud.
- POST `/api/open-folder` JSON `{id:string}` -> `{ok:true}`; opens only the job output folder locally.
- GET `/api/download?id=ID&format=txt|timestamps|srt|json` -> attachment bytes. fetch with token, create Blob URL, download using fixed filename.
- Error: non-2xx `{error:"Readable message"}`. Network/HTTP failures need visible error and controls must recover.

Poll state every 1.5–2 seconds, job detail for selected/active job. Empty initial state, loading/running/stopping, failed, completed, historical selection and download errors must work. No fake jobs/progress/text in production UI. UI language English, with clear terms; Russian README explains usage. Responsive, keyboard accessible, reduced motion preference. Visual direction: polished mostly-black/white desktop utility, restrained Orca-inspired shell, elegant typography, subtle borders, clear main action, useful whitespace and compact sidebar. Generic original logo; do not copy Orca assets or imply affiliation.
