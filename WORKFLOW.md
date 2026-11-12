STRICT SCOPE
- Only edit or create files inside engine/. Never touch anything outside it (shared/, api/, simulator/, dashboard/, patients/, tools/, README.md, requirements.txt, .claude/). If something outside needs changing, stop and write a "REQUEST FOR TEAM:" message instead.
- Contracts never change.

STYLE
- I am a beginner. Explain everything in very simple words. Edit files directly; after each change tell me in 2-4 simple lines what changed.

GIT (my repo is INSIDE engine/, branch engine-work, remote origin)
- Run git only as `git -C engine <command>` from the project root. Never run git commands at the project root.
- Make small commits with short messages like "B1: ignore sudden spikes". Commit only when tests pass.
- Commits are authored only by me (Shrexxx-sys). Never add Co-Authored-By or any mention of Claude/AI/Anthropic to commit messages. After every commit, run `git -C engine log -1 --format=%B` and `git -C engine log -1 --format="%an <%ae>"`. If a trailer or a different author appears, fix it with `git -C engine commit --amend` BEFORE pushing.
- Before every commit run `git -C engine status` and make sure only files inside engine/ are included.
- After a passing commit, push with `git -C engine push origin engine-work`. Never push to main. Never force push. If push fails, show me the error and stop.

TEST-FIX LOOP (for every task)
1. Make the change.
2. Run `python -m engine.test_engine` and `python -m tools.offline_run` from the project root.
3. If anything crashes, fails or misses a target: find the root cause, fix it, re-run. Repeat up to 6 rounds.
4. Never weaken a test, hide an error, or change a target just to get green. If a threshold must change, explain why and say it is an engineering choice, not a clinical fact.
5. If still failing after 6 rounds, stop, show me the evidence and ask me.
6. Only when everything passes: commit, verify the commit (see GIT), push.
7. After each task, give me a short summary and the current offline_run table. Remind me the project is clinical decision support, not diagnosis.
