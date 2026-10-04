name: Request ArmorCode Exception

# Triggers when someone comments on a PR. Both jobs only act on comments
# that start with /request-exception on a pull request.
on:
  issue_comment:
    types: [created]

jobs:
  # Job 1: create or reuse an exception in ArmorCode and attach the findings.
  request-exception:
    if: >
      github.event.issue.pull_request &&
      startsWith(github.event.comment.body, '/request-exception')
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: read
    steps:
      - name: Checkout (needed only if you keep the script in-repo)
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install requests

      - name: Request exception in ArmorCode
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          # Uses a dedicated API key if you created one, else the existing secret.
          ARMORCODE_API_TOKEN: ${{ secrets.ARMORCODE_EXCEPTION_API_TOKEN || secrets.ARMORCODE_API_TOKEN }}
          ARMORCODE_BASE_URL: ${{ vars.ARMORCODE_BASE_URL }}
          EXCEPTION_ENVIRONMENT: ${{ vars.ARMORCODE_ENV }}
          PR_NUMBER: ${{ github.event.issue.number }}
          REPO: ${{ github.repository }}
          COMMENT_BODY: ${{ github.event.comment.body }}
          COMMENT_AUTHOR: ${{ github.event.comment.user.login }}
        run: python .github/scripts/request_exception.py

  # Job 2: record a Release Gate entry in ArmorCode for the same request.
  # Runs after job 1 whether it passed or failed, and is skipped for
  # comments that job 1 ignored.
  release-gate:
    needs: request-exception
    if: ${{ !cancelled() && needs.request-exception.result != 'skipped' }}
    runs-on: ubuntu-latest
    permissions:
      checks: write          # For writing to the checks tab
      pull-requests: write   # For commenting on PRs
      contents: read         # For accessing repo content
    steps:
      - name: Run ArmorCode Release Gate
        uses: docker://public.ecr.aws/armor-code/armorcode-release-gate:latest
        with:
          # Defaults to the repo name; set repo variables to override.
          product: ${{ vars.ARMORCODE_PRODUCT || github.event.repository.name }}
          subProduct: ${{ vars.ARMORCODE_SUBPRODUCT || github.event.repository.name }}
          env: ${{ vars.ARMORCODE_ENV || 'Production' }}
          armorcodeHost: ${{ vars.ARMORCODE_BASE_URL || 'https://app.armorcode.com' }}
          mode: 'warn'
          # Release Gate expects a CI/CD key. Uses a dedicated secret if present,
          # else the existing secret.
          armorcodeAPIToken: ${{ secrets.ARMORCODE_CICD_TOKEN || secrets.ARMORCODE_API_TOKEN }}
          githubToken: ${{ github.token }}
          additionalAQLFilters: ""
