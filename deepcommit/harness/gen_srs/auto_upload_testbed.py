#!/usr/bin/env python3
"""
Automatically create GitHub repository and upload testbed.

This script:
1. Creates a GitHub repository in the organization
2. Pushes the main branch
3. Pushes all tags

Usage:
    python scripts/auto_upload_testbed.py --token YOUR_GITHUB_TOKEN

    or set GITHUB_TOKEN environment variable:
    export GITHUB_TOKEN=your_token_here
    python scripts/auto_upload_testbed.py
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import requests


class GitHubRepoUploader:
    def __init__(self, token: str, org: str, repo_name: str, testbed_path: str, use_https: bool = True):
        self.token = token
        self.org = org
        self.repo_name = repo_name
        self.testbed_path = Path(testbed_path)
        self.use_https = use_https
        self.api_base = "https://api.github.com"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def create_repository(self, description: str, private: bool = False):
        """Create a new repository in the organization."""
        url = f"{self.api_base}/orgs/{self.org}/repos"

        data = {
            "name": self.repo_name,
            "description": description,
            "private": private,
            "auto_init": False,  # Don't create README
            "has_issues": True,
            "has_projects": False,
            "has_wiki": False,
        }

        print(f"Creating repository {self.org}/{self.repo_name}...")
        response = requests.post(url, headers=self.headers, json=data)

        if response.status_code == 201:
            print("✓ Repository created successfully")
            return response.json()
        elif response.status_code == 422:
            # Repository already exists
            print("✓ Repository already exists")
            return self.get_repository()
        else:
            print(f"✗ Failed to create repository: {response.status_code}")
            print(f"Response: {response.text}")
            sys.exit(1)

    def get_repository(self):
        """Get repository information."""
        url = f"{self.api_base}/repos/{self.org}/{self.repo_name}"
        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            return response.json()
        else:
            print(f"✗ Repository not found: {response.status_code}")
            return None

    def run_command(self, cmd: list, cwd: str = None, check: bool = True):
        """Run a shell command."""
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            cwd=cwd or str(self.testbed_path),
            capture_output=True,
            text=True,
        )

        if check and result.returncode != 0:
            print(f"✗ Command failed with exit code {result.returncode}")
            print(f"stderr: {result.stderr}")
            sys.exit(1)

        return result

    def setup_git_remote(self):
        """Setup or update Git remote."""
        print("\nSetting up Git remote...")

        # Check if remote exists
        result = self.run_command(["git", "remote", "get-url", "origin"], check=False)

        # Choose remote URL based on use_https flag
        if self.use_https:
            remote_url = f"https://{self.token}@github.com/{self.org}/{self.repo_name}.git"
            display_url = f"https://***@github.com/{self.org}/{self.repo_name}.git"
        else:
            remote_url = f"git@github.com:{self.org}/{self.repo_name}.git"
            display_url = remote_url

        if result.returncode == 0:
            # Remote exists, update it
            print(f"Updating remote to: {display_url}")
            self.run_command(["git", "remote", "set-url", "origin", remote_url])
        else:
            # Add new remote
            print(f"Adding remote: {display_url}")
            self.run_command(["git", "remote", "add", "origin", remote_url])

        print(f"✓ Remote set to: {display_url}")

    def push_to_github(self):
        """Push main branch and all tags to GitHub."""
        print("\nPushing to GitHub...")

        # Push main branch
        print("Pushing main branch...")
        self.run_command(["git", "push", "-u", "origin", "main"])
        print("✓ Main branch pushed")

        # Push tags
        print("Pushing all tags...")
        result = self.run_command(["git", "tag"], check=False)
        tag_count = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
        print(f"Found {tag_count} tags")

        self.run_command(["git", "push", "--tags"])
        print(f"✓ All {tag_count} tags pushed")

    def disable_github_actions(self):
        """Disable GitHub Actions for the repository."""
        print("\nDisabling GitHub Actions...")

        url = f"{self.api_base}/repos/{self.org}/{self.repo_name}/actions/permissions"

        data = {"enabled": False}

        response = requests.put(url, headers=self.headers, json=data)

        if response.status_code == 204:
            print("✓ GitHub Actions disabled successfully")
            return True
        else:
            print(f"⚠ Failed to disable GitHub Actions: {response.status_code}")
            print(f"  Response: {response.text}")
            print("  You can manually disable it at:")
            print(f"  https://github.com/{self.org}/{self.repo_name}/settings/actions")
            return False

    def verify_upload(self):
        """Verify the upload was successful."""
        print("\nVerifying upload...")

        # Get repository info
        repo_info = self.get_repository()
        if not repo_info:
            print("✗ Failed to verify repository")
            return False

        print(f"✓ Repository URL: {repo_info['html_url']}")
        print(f"✓ Clone URL: {repo_info['clone_url']}")

        # Get tags count from API
        url = f"{self.api_base}/repos/{self.org}/{self.repo_name}/tags"
        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            tags = response.json()
            print(f"✓ Tags on GitHub: {len(tags)}")

        return True

    def run(self, description: str, private: bool = False):
        """Run the complete upload process."""
        print("=" * 70)
        print("Automated GitHub Repository Upload")
        print(f"Organization: {self.org}")
        print(f"Repository: {self.repo_name}")
        print(f"Testbed: {self.testbed_path}")
        print("=" * 70)

        # Step 1: Create repository
        print("\nStep 1: Creating GitHub repository...")
        self.create_repository(description, private)

        # Step 2: Setup Git remote
        print("\nStep 2: Setting up Git remote...")
        self.setup_git_remote()

        # Step 3: Push to GitHub
        print("\nStep 3: Pushing to GitHub...")
        self.push_to_github()

        # Step 4: Disable GitHub Actions
        print("\nStep 4: Disabling GitHub Actions...")
        self.disable_github_actions()

        # Step 5: Verify
        print("\nStep 5: Verifying upload...")
        self.verify_upload()

        print("\n" + "=" * 70)
        print("✓ Upload completed successfully!")
        print("=" * 70)
        print("\nView repository at:")
        print(f"  https://github.com/{self.org}/{self.repo_name}")
        print("\nView tags at:")
        print(f"  https://github.com/{self.org}/{self.repo_name}/tags")


def main():
    parser = argparse.ArgumentParser(
        description="Automatically create GitHub repository and upload testbed",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using GITHUB_TOKEN environment variable
  export GITHUB_TOKEN=ghp_xxxxxxxxxxxx
  python scripts/auto_upload_testbed.py

  # Passing token as argument
  python scripts/auto_upload_testbed.py --token ghp_xxxxxxxxxxxx

  # Custom repository name
  python scripts/auto_upload_testbed.py --repo-name my-custom-testbed

  # Private repository
  python scripts/auto_upload_testbed.py --private

Note: You need a GitHub personal access token with 'repo' scope.
Create one at: https://github.com/settings/tokens/new
        """,
    )

    parser.add_argument(
        "--token",
        help="GitHub personal access token (or set GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--org",
        default="DeepCommit-Bench",
        help="GitHub organization name (default: DeepCommit-Bench)",
    )
    parser.add_argument(
        "--repo-name",
        default="urllib3_urllib3_2.0.6_2.3.0_test_multi_stage_V2",
        help="Repository name",
    )
    parser.add_argument(
        "--testbed-path",
        default="DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/testbed",
        help="Path to testbed directory",
    )
    parser.add_argument(
        "--description",
        default="urllib3 testbed with milestones for version 2.0.6 to 2.3.0 (test_multi_stage_V2)",
        help="Repository description",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        default=True,
        help="Create as private repository (default: private, use --no-private for public)",
    )
    parser.add_argument(
        "--no-private",
        dest="private",
        action="store_false",
        help="Create as public repository",
    )

    args = parser.parse_args()

    # Get token from argument or environment
    token = args.token or os.getenv("GITHUB_TOKEN")
    if not token:
        print("Error: GitHub token required!")
        print("  Set GITHUB_TOKEN environment variable, or")
        print("  Pass --token argument")
        print("\nCreate a token at: https://github.com/settings/tokens/new")
        print("Required scopes: repo")
        sys.exit(1)

    # Create uploader and run
    uploader = GitHubRepoUploader(
        token=token,
        org=args.org,
        repo_name=args.repo_name,
        testbed_path=args.testbed_path,
        use_https=True,  # Always use HTTPS for authentication
    )

    uploader.run(description=args.description, private=args.private)


if __name__ == "__main__":
    main()
