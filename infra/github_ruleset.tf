# --- Branch protection: the main branch ----------------------------------------
#
# A ruleset (not the legacy branch-protection API): free on public repos, and
# the shape the GitHub UI now offers under Settings -> Rules -> Rulesets.
#
# Required checks are the CI jobs that run on pull_request. The `audit` job is
# deliberately absent: ci.yml skips it on PRs (`if: github.event_name !=
# 'pull_request'`), so requiring it would leave every PR waiting on a check
# that never reports.
#
# No bypass_actors: the repository admin is also the only committer, so an
# "admin may bypass" entry would make the whole ruleset advisory. For a genuine
# emergency, flip `enforcement` to "disabled", push, and flip it back.

resource "github_repository_ruleset" "main" {
  name        = "main"
  repository  = var.github_repo
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["~DEFAULT_BRANCH"]
      exclude = []
    }
  }

  rules {
    # No force-pushes, no deleting main.
    non_fast_forward = true
    deletion         = true

    pull_request {
      # Solo repo: an author cannot approve their own PR, so any non-zero
      # count would block every merge.
      required_approving_review_count   = 0
      dismiss_stale_reviews_on_push     = true
      require_last_push_approval        = false
      required_review_thread_resolution = true
    }

    required_status_checks {
      # false = a PR need not be rebased onto the newest main before merging.
      strict_required_status_checks_policy = false

      required_check {
        context = "lint"
      }
      required_check {
        context = "types"
      }
      required_check {
        context = "test"
      }
    }
  }
}
