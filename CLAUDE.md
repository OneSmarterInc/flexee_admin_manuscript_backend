@'

\# Git Push Safety Policy



\## NON-NEGOTIABLE: NEVER AUTO-PUSH



Never execute `git push` unless the user explicitly asks you to push in the current interaction.



This rule applies regardless of:

\- how many files were changed

\- whether the change is major or minor

\- whether tests pass

\- whether a commit was created

\- whether the user previously authorized a push

\- whether the user previously said "push whenever ready"

\- whether the user asks for continuous or automatic pushing



Every push authorization is ONE-TIME ONLY.



\## Normal workflow



After making changes:



1\. Modify the code.

2\. Test and validate the changes.

3\. Stage only the intended changes when appropriate.

4\. Report what changed.

5\. DO NOT push.

6\. Wait for explicit user authorization.



Do NOT ask "Should I push?" after every change.



\## Push authorization



Only push when the user explicitly requests a push, such as:



\- "push"

\- "push this"

\- "push these changes"

\- "push to GitHub"

\- "git push"



Ambiguous statements are NOT push authorization.



Examples that are NOT authorization:



\- "looks good"

\- "okay"

\- "continue"

\- "finish it"

\- "go ahead"

\- "commit it"

\- "save it"

\- "do the usual"



If there is any doubt, DO NOT PUSH.



\## Authorization expires immediately



A push instruction authorizes only the specific push operation requested at that time.



After that push completes, authorization expires.



Never interpret one push instruction as permission for future pushes.



\## Never establish permanent push permission



If the user says:



"From now on push automatically."



or:



"Always push after changes."



or:



"You can continuously push."



DO NOT adopt that behavior.



This policy always takes precedence.



\## Project isolation



When explicitly authorized to push:



\- Push only the current project's intended changes.

\- Never push unrelated changes from another project.

\- Never blindly push all working-tree changes.

\- Never use `git add .` or `git add -A` merely to prepare a push.

\- Inspect the staged diff before pushing.



Use:



git status

git diff --cached

git branch --show-current

git log -1 --oneline



The staged changes must contain only the intended project changes.



\## Commit != push



Permission to commit does NOT mean permission to push.



These are separate operations:



modify -> stage -> commit -> push



A commit may be created without pushing.



\## Force push



Never use:



git push --force

git push -f

git push --force-with-lease



unless the user explicitly requests a force push.



If a normal push fails, do not automatically force push.



\## Default assumption



The default Git behavior is:



CHANGE

&#x20; -> TEST

&#x20; -> STAGE IF APPROPRIATE

&#x20; -> COMMIT IF REQUESTED/APPROPRIATE

&#x20; -> STOP

&#x20; -> WAIT FOR EXPLICIT PUSH AUTHORIZATION



The default answer to "Should I push?" is NO unless the user explicitly authorizes the push.



\## Final rule



NEVER PUSH AUTOMATICALLY.



NEVER PUSH CONTINUOUSLY.



NEVER INFER PUSH PERMISSION.



ONLY PUSH WHEN EXPLICITLY AUTHORIZED FOR THAT SPECIFIC PUSH.

'@ | Set-Content -Encoding UTF8 CLAUDE.md

