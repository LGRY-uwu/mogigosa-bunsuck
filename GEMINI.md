# 프로젝트 Git 자동 동기화 규칙

1. 앞으로 내 모든 코드 수정이나 파일 생성 요청을 처리하기 직전에, 반드시 터미널에서 `git pull origin main`을 먼저 실행해 최신 코드를 동기화해라.
2. 코드 수정과 작업이 완료되면, 나에게 최종 완료 답변을 출력하기 직전에 반드시 터미널에서 `git add .`, `git commit -m "AI 자동 업데이트"`, `git push origin main`을 순서대로 실행해 GitHub에 업로드해라.
3. 만약 `git pull`이나 `git push` 과정에서 충돌(Conflict)이나 에러가 발생하면 임의로 강제 푸시(`--force`)하지 말고 즉시 멈춘 뒤 나에게 상황을 보고해라.
