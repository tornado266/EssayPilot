# Third-party notices and design provenance

EssayPilot's IELTS scoring redesign is independently implemented from public IELTS assessment materials. IELTS is a trademark of its respective owners; EssayPilot is not affiliated with, approved by, or endorsed by IELTS. Scores produced by the product are estimated practice bands, not official results.

## IELTS public assessment materials

The operational references under `skills/ielts-writing/references/` paraphrase the public IELTS Writing Band Descriptors (updated May 2023) and Key Assessment Criteria. They link to the original sources and do not reproduce the official documents in full.

## MIT-licensed implementation references

- `xuchi-0808/ielts-claude-skills` — MIT License. An earlier EssayPilot skill closely followed this project. The redesigned skill is independently rewritten; this notice preserves attribution for the historical influence.
- `Shpaldik/OpenIELTS-AI` — MIT License. Reviewed for high-level ideas about structured output and separation of configuration from execution; no source code was copied in this redesign.
- `NayHtetWin/ielts_writing_evaluator` — MIT License. Reviewed for high-level ideas about rubric injection and typed output; no source code was copied in this redesign.

The full MIT license texts and copyright notices remain available in the respective upstream repositories. If code is copied from any of these projects in future, its applicable notice must be included with the copied material.

### Historical `ielts-claude-skills` MIT notice

MIT License

Copyright (c) 2026 xuchi

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Repositories without a clear licence

- `hubeiqiao/ielts-celpip-writing-skills`
- `Bosh-Kuo/awesome-agent-toolkit`

Only general ideas were considered, including uncertainty-aware estimates, evidence-led feedback, and separating concise skill instructions from reference material. No wording, tables, or source code was copied.

This notice does not grant a licence for EssayPilot itself. The project owner must choose and publish any repository-level code licence separately.

## Alpine UI photography

- **Snow-covered mountains under a clear, bright sky** — photograph by Tim Arnold, captured in Crans-Montana, Switzerland. Source: https://unsplash.com/photos/cNb7hPlkItg
- Used as the local EssayPilot Alpine / Summit Hero image under the Unsplash License: https://unsplash.com/license

The repository includes optimized JPEG and WebP derivatives for application use. It does not use the photograph as a standalone resale item or as part of a competing image library.

## UI design references

The Alpine UI uses independently written HTML and CSS. Its restrained loader, Bento layout, stepper, and entrance transitions were informed at a pattern level by Uiverse, Aceternity UI, and React Bits. No React package, remote script, copied component implementation, or third-party runtime dependency is included.

- Uiverse: https://uiverse.io/
- Aceternity UI: https://ui.aceternity.com/components
- React Bits: https://reactbits.dev/
