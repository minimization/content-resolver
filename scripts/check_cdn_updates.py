#!/usr/bin/env python3
"""
Check for updates to CDN dependencies in templates/layout.html
Queries npm registry API to get latest versions without requiring npm installation
Includes compatibility checking to prevent breaking updates
"""

import base64
import hashlib
import re
import sys
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

NPM_REGISTRY_HOST = "https://registry.npmjs.org"
# Cache for package metadata to avoid repeated API calls
_package_metadata_cache = {}


def get_package_metadata(package_name: str, version: str) -> Optional[Dict]:
    """Get package metadata from npm registry for a specific version"""
    cache_key = f"{package_name}@{version}"
    data = None

    if cache_key in _package_metadata_cache:
        return _package_metadata_cache[cache_key]

    url = f"{NPM_REGISTRY_HOST}/{package_name}/{version}"
    try:
        response = requests.get(url, timeout=10)

        response.raise_for_status()
        data = response.json()
        _package_metadata_cache[cache_key] = data

    except Exception as e:
        print(
            f"  Warning: Could not fetch metadata for {package_name}@{version}: {type(e).__name__}",
            file=sys.stderr,
        )
    return data


def check_semver_constraint(version: str, constraint: str) -> bool:
    """
    Simple semver constraint checker
    Supports: ^1.2.3, ~1.2.3, >=1.2.3, >1.2.3, <=1.2.3, <1.2.3, 1.2.3 - 2.3.4, *
    """
    constraint = constraint.strip()

    # Handle wildcard
    if constraint == "*":
        return True

    # Handle hyphen ranges (e.g., "1.9.1 - 3" or "1.2.3 - 2.3.4")
    if " - " in constraint:
        parts = constraint.split(" - ")
        if len(parts) == 2:
            min_ver = parts[0].strip()
            max_ver = parts[1].strip()

            # Handle shorthand like "1.9.1 - 3" (means "1.9.1 - 3.x.x")
            # Normalize: if max_ver is a single digit, treat as major version
            if "." not in max_ver:
                # "1.9.1 - 3" means >= 1.9.1 and < 4.0.0
                max_major = int(max_ver)
                # Check if version is in range [min_ver, max_major+1.0.0)
                return version_compare(version, min_ver) >= 0 and parse_version(version)[0] <= max_major
            else:
                # Full version range
                return version_compare(version, min_ver) >= 0 >= version_compare(version, max_ver)

    # Handle caret (^) - compatible with version
    if constraint.startswith("^"):
        target = constraint[1:]
        target_parts = parse_version(target)
        version_parts = parse_version(version)
        # ^1.2.3 allows >= 1.2.3 and < 2.0.0
        if target_parts[0] > 0:
            return version_parts[0] == target_parts[0] and version_compare(version, target) >= 0
        # ^0.2.3 allows >= 0.2.3 and < 0.3.0
        return (
            version_parts[0] == target_parts[0]
            and version_parts[1] == target_parts[1]
            and version_compare(version, target) >= 0
        )

    # Handle tilde (~) - patch-level changes
    if constraint.startswith("~"):
        target = constraint[1:]
        target_parts = parse_version(target)
        version_parts = parse_version(version)
        # ~1.2.3 allows >= 1.2.3 and < 1.3.0
        return (
            version_parts[0] == target_parts[0]
            and version_parts[1] == target_parts[1]
            and version_compare(version, target) >= 0
        )

    # Handle operators
    for op in [">=", "<=", ">", "<"]:
        if constraint.startswith(op):
            target = constraint[len(op) :].strip()
            cmp = version_compare(version, target)
            if op == ">=":
                return cmp >= 0
            elif op == "<=":
                return cmp <= 0
            elif op == ">":
                return cmp > 0
            elif op == "<":
                return cmp < 0

    # Exact version match
    return version == constraint


def get_peer_dependencies(package_name: str, version: str) -> Dict[str, str]:
    """Get peerDependencies for a package version"""
    metadata = get_package_metadata(package_name, version)
    peer_deps = {}
    if metadata:
        peer_deps = metadata.get("peerDependencies", {})
    return peer_deps


def parse_version(version: str) -> Tuple[int, int, int]:
    """Parse semantic version string to tuple for comparison"""
    semantic_version = 0, 0, 0
    try:
        parts = version.split(".")
        semantic_version = (
            int(parts[0]),
            int(parts[1]),
            int(parts[2]) if len(parts) > 2 else 0,
        )
    except ValueError, IndexError:
        pass
    return semantic_version


def version_compare(v1: str, v2: str) -> int:
    """Compare two version strings. Returns: -1 (v1<v2), 0 (equal), 1 (v1>v2)"""
    t1 = parse_version(v1)
    t2 = parse_version(v2)
    if t1 < t2:
        return -1
    elif t1 > t2:
        return 1
    return 0


def get_latest_version(package_name: str) -> str:
    """Get latest version from npm registry API"""
    url = f"{NPM_REGISTRY_HOST}/{package_name}/latest"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("version", "unknown")
    except Exception as e:
        print(f"Error fetching {package_name}: {e}", file=sys.stderr)
        return "unknown"


def get_latest_in_major_version(package_name: str, major_version: int) -> Optional[str]:
    """Get the latest version within a specific major version line"""
    url = f"{NPM_REGISTRY_HOST}/{package_name}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        versions = list(data.get("versions", {}).keys())

        # Filter to stable versions in the same major version

        major_versions = [v for v in versions if re.match(rf"^{major_version}\.\d+\.\d+$", v)]

        if major_versions:
            major_versions.sort(key=lambda v: parse_version(v), reverse=True)
            return major_versions[0]

        return None
    except Exception as e:
        print(f"Error fetching major version for {package_name}: {e}", file=sys.stderr)
        return None


def get_compatible_version(package_name: str, max_version: Optional[str] = None) -> str:
    """Get the latest compatible version below max_version"""
    if not max_version:
        return get_latest_version(package_name)

    url = f"{NPM_REGISTRY_HOST}/{package_name}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        versions = list(data.get("versions", {}).keys())

        # Filter to versions below max_version
        compatible = [v for v in versions if version_compare(v, max_version) < 0]

        # Sort and return latest
        if compatible:
            compatible.sort(key=lambda v: parse_version(v), reverse=True)
            return compatible[0]

        return "unknown"
    except Exception as e:
        print(
            f"Error fetching compatible version for {package_name}: {e}",
            file=sys.stderr,
        )
        return "unknown"


def verify_url_exists(url: str) -> bool:
    """Verify that a CDN URL exists and returns 200 OK"""
    try:
        response = requests.head(url, timeout=10, allow_redirects=True)
        return response.status_code == 200
    except Exception as e:
        print(
            f"  ⚠️  URL verification failed: {url[:80]}... ({type(e).__name__})",
            file=sys.stderr,
        )
        return False


def calculate_sri_hash(url: str) -> Optional[str]:
    """
    Download file from URL and calculate SHA-384 SRI hash
    Returns the integrity attribute value (e.g., "sha384-...")
    """

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        # Calculate SHA-384 hash
        sha384_hash = hashlib.sha384(response.content).digest()

        # Base64 encode
        b64_hash = base64.b64encode(sha384_hash).decode("utf-8")

        return f"sha384-{b64_hash}"
    except Exception as e:
        print(
            f"  ⚠️  Failed to calculate SRI hash for {url[:80]}...: {type(e).__name__}",
            file=sys.stderr,
        )
        return None


def flatten_jsdelivr_files(files_tree: List, path_prefix: str = "") -> List[str]:
    """Recursively flatten jsDelivr file tree into list of paths"""
    result = []
    for item in files_tree:
        item_name = item.get("name", "")
        item_type = item.get("type", "")
        full_path = f"{path_prefix}/{item_name}"

        if item_type == "file":
            result.append(full_path)
        elif item_type == "directory" and "files" in item:
            result.extend(flatten_jsdelivr_files(item["files"], full_path))

    return result


def get_jsdelivr_files(package_name: str, version: str) -> Dict[str, str]:
    """
    Get available files for a package from jsDelivr CDN
    Returns dict mapping file types to CDN URLs
    """
    url = f"https://data.jsdelivr.com/v1/packages/npm/{package_name}@{version}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Flatten the nested file tree
        all_files = flatten_jsdelivr_files(data.get("files", []))

        files = {}

        # Priority patterns for different file types
        js_patterns = [
            (r"/dist/js/.*\.bundle\.min\.js$", "js"),  # Bootstrap-style bundled
            (r"/dist/.*\.bundle\.min\.js$", "js"),  # General bundled JS
            (r"/dist/js/bootstrap\.min\.js$", "js"),  # Bootstrap main file
            (r"/dist/js/.*\.min\.js$", "js"),  # Bootstrap-style dist/js
            (r"/dist/.*\.min\.js$", "js"),  # General dist minified
            (r"/.*\.min\.js$", "js"),  # Any minified JS
        ]

        css_patterns = [
            (r"/dist/css/bootstrap\.min\.css$", "css"),  # Bootstrap main CSS
            (r"/dist/css/.*\.min\.css$", "css"),  # Bootstrap-style dist/css
            (r"/dist/.*\.min\.css$", "css"),  # General dist minified
            (r"/.*\.min\.css$", "css"),  # Any minified CSS
        ]

        # Search for JS files
        for pattern, key in js_patterns:
            if key in files:
                continue
            for file_path in all_files:
                if re.search(pattern, file_path):
                    # Exclude .map files and test files
                    if ".map" not in file_path and "/test" not in file_path:
                        cdn_url = f"https://cdn.jsdelivr.net/npm/{package_name}@{version}{file_path}"
                        files[key] = cdn_url
                        break

        # Search for CSS files
        for pattern, key in css_patterns:
            if key in files:
                continue
            for file_path in all_files:
                if re.search(pattern, file_path):
                    if ".map" not in file_path:
                        cdn_url = f"https://cdn.jsdelivr.net/npm/{package_name}@{version}{file_path}"
                        files[key] = cdn_url
                        break

        return files
    except Exception as e:
        print(
            f"  Warning: Could not fetch jsDelivr files for {package_name}@{version}: {type(e).__name__}",
            file=sys.stderr,
        )
        return {}


def find_updated_cdn_url(package_name: str, version: str, current_url: str) -> Optional[str]:
    """
    Find the updated CDN URL for a package version

    Tries multiple strategies:
    1. jsDelivr API (reliable, mirrors all npm packages)
    2. Simple version replacement (fallback)
    """
    # Determine file type from current URL
    is_css = current_url.endswith(".css") or "/css/" in current_url

    # Strategy 1: Try jsDelivr API
    jsdelivr_files = get_jsdelivr_files(package_name, version)

    if is_css and "css" in jsdelivr_files:
        return jsdelivr_files["css"]
    elif not is_css and "js" in jsdelivr_files:
        return jsdelivr_files["js"]

    # Strategy 2: Try version replacement (fallback)
    # Extract current version from URL
    version_pattern = r"\d+\.\d+\.\d+"
    matches = list(re.finditer(version_pattern, current_url))

    if matches:
        # Replace the last version number found (most likely the package version)
        old_version = matches[-1].group()
        updated_url = current_url.replace(old_version, version)
        return updated_url

    return None


def extract_version_from_url(url: str, package_name: str) -> Optional[str]:
    """Extract version number from CDN URL"""
    patterns = [
        rf"{package_name}@(\d+\.\d+\.\d+)",
        rf"{package_name}[/-](\d+\.\d+\.\d+)",
        rf"{package_name}\.js[/@](\d+\.\d+\.\d+)",
        r"bootstrap(?:cdn)?/(?:bootstrap/)?(\d+\.\d+\.\d+)",  # Bootstrap specific
        r"datatables\.net[@/](\d+\.\d+\.\d+)",  # DataTables specific (@ or /)
    ]

    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def parse_layout_html(file_path: str) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """Parse layout.html using BeautifulSoup to extract CDN versions and URLs

    Returns:
        Tuple of (versions dict, urls dict) where:
        - versions: package_name -> version
        - urls: package_name -> list of CDN URLs (CSS, JS, etc.)
    """
    with open(file_path, "r") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    versions = {}
    urls = {}

    # Find all script and link tags with CDN URLs
    cdn_tags = []
    cdn_tags.extend(soup.find_all("script", src=True))
    cdn_tags.extend(soup.find_all("link", href=True))

    for tag in cdn_tags:
        url = tag.get("src") or tag.get("href")
        if not url:
            continue

        url_lower = url.lower()

        # Bootstrap
        if "bootstrap" in url_lower:
            version = extract_version_from_url(url, "bootstrap")
            if version:
                if "bootstrap" not in versions:
                    versions["bootstrap"] = version
                    urls["bootstrap"] = []
                urls["bootstrap"].append(url)

        # DataTables (check before jQuery since files may contain "jquery" in name)
        elif "datatables" in url_lower:
            version = extract_version_from_url(url, "datatables")
            if version:
                if "datatables.net" not in versions:
                    versions["datatables.net"] = version
                    urls["datatables.net"] = []
                urls["datatables.net"].append(url)

        # jQuery (check after DataTables to avoid false positives)
        elif "jquery" in url_lower:
            version = extract_version_from_url(url, "jquery")
            if version:
                if "jquery" not in versions:
                    versions["jquery"] = version
                    urls["jquery"] = []
                urls["jquery"].append(url)

        # Chart.js
        elif "chart.js" in url_lower:
            version = extract_version_from_url(url, "chart")
            if version:
                if "chart.js" not in versions:
                    versions["chart.js"] = version
                    urls["chart.js"] = []
                urls["chart.js"].append(url)

        # Chart.js adapter
        elif "chartjs-adapter-date-fns" in url_lower:
            version = extract_version_from_url(url, "chartjs-adapter-date-fns")
            if version:
                if "chartjs-adapter-date-fns" not in versions:
                    versions["chartjs-adapter-date-fns"] = version
                    urls["chartjs-adapter-date-fns"] = []
                urls["chartjs-adapter-date-fns"].append(url)

        # Axios
        elif "axios" in url_lower:
            version = extract_version_from_url(url, "axios")
            if version:
                if "axios" not in versions:
                    versions["axios"] = version
                    urls["axios"] = []
                urls["axios"].append(url)

        # Flatpickr
        elif "flatpickr" in url_lower:
            version = extract_version_from_url(url, "flatpickr")
            if version:
                if "flatpickr" not in versions:
                    versions["flatpickr"] = version
                    urls["flatpickr"] = []
                urls["flatpickr"].append(url)

    return versions, urls


def check_compatibility(
    package: str,
    proposed_version: str,
    all_current_versions: Dict[str, str],
    all_latest_versions: Dict[str, str],
) -> Optional[Dict]:
    """
    Check if proposed update is compatible with other dependencies
    Uses npm peerDependencies to dynamically determine compatibility
    Checks against LATEST versions of other packages, not current versions
    """
    # Manual compatibility rules for cases not covered by npm peerDependencies
    # jQuery 4.x is not recommended with Bootstrap 5 or DataTables 2
    if package == "jquery":
        proposed_major = parse_version(proposed_version)[0]
        if proposed_major >= 4:
            # Check if Bootstrap 5+ or DataTables 2+ is present
            for other_pkg in all_latest_versions.keys():
                if "bootstrap" in other_pkg.lower():
                    bootstrap_version = all_latest_versions[other_pkg]
                    bootstrap_major = parse_version(bootstrap_version)[0]
                    if bootstrap_major >= 5:
                        # Recommend latest jQuery 3.x instead
                        compatible_version = get_latest_in_major_version("jquery", 3)
                        return {
                            "incompatible": True,
                            "conflicting_package": other_pkg,
                            "conflicting_version": bootstrap_version,
                            "reason": f"jQuery 4.x is not recommended with Bootstrap 5+. Bootstrap 5 optional jQuery integration may not work correctly.",
                            "docs": "https://getbootstrap.com/docs/5.3/getting-started/javascript/",
                            "suggested_version": compatible_version,
                        }
                elif "datatables" in other_pkg.lower():
                    datatables_version = all_latest_versions[other_pkg]
                    datatables_major = parse_version(datatables_version)[0]
                    if datatables_major >= 2:
                        # Recommend latest jQuery 3.x instead
                        compatible_version = get_latest_in_major_version("jquery", 3)
                        return {
                            "incompatible": True,
                            "conflicting_package": other_pkg,
                            "conflicting_version": datatables_version,
                            "reason": f"jQuery 4.x is not recommended with DataTables 2.x. DataTables 2 is tested with jQuery 3.x.",
                            "docs": "https://datatables.net/download/",
                            "suggested_version": compatible_version,
                        }

    # Get peerDependencies for the proposed version
    peer_deps = get_peer_dependencies(package, proposed_version)

    for peer_pkg, peer_constraint in peer_deps.items():
        # Normalize package names (e.g., "jquery" might be listed as "jQuery")
        peer_pkg_lower = peer_pkg.lower()

        # Check if this peer dependency is in our packages
        matching_pkg = None
        for current_pkg in all_current_versions.keys():
            if current_pkg.lower() == peer_pkg_lower or peer_pkg_lower in current_pkg.lower():
                matching_pkg = current_pkg
                break

        if not matching_pkg:
            continue

        # Use the LATEST version of the other package for compatibility check
        other_latest_version = all_latest_versions.get(matching_pkg, all_current_versions[matching_pkg])

        # Check if latest version satisfies the peer dependency constraint
        if not check_semver_constraint(other_latest_version, peer_constraint):
            # Try to find a compatible version
            compatible = None
            # For simple constraints, try to extract max version
            if peer_constraint.startswith("<"):
                max_ver = peer_constraint.lstrip("<= ").strip()
                compatible = get_compatible_version(package, max_ver)

            return {
                "incompatible": True,
                "conflicting_package": matching_pkg,
                "conflicting_version": other_latest_version,
                "reason": f"{package} {proposed_version} requires {peer_pkg} {peer_constraint}, but {matching_pkg} {other_latest_version} does not satisfy this",
                "docs": f"https://www.npmjs.com/package/{package}/v/{proposed_version}",
                "suggested_version": (compatible if compatible and compatible != "unknown" else None),
            }

    # Also check reverse: do the latest versions of other packages have peer deps on this package?
    for other_pkg in all_current_versions.keys():
        if other_pkg == package:
            continue

        # Check against LATEST version of the other package
        other_latest_version = all_latest_versions.get(other_pkg, all_current_versions[other_pkg])
        other_peer_deps = get_peer_dependencies(other_pkg, other_latest_version)

        for peer_pkg, peer_constraint in other_peer_deps.items():
            peer_pkg_lower = peer_pkg.lower()
            if peer_pkg_lower == package.lower() or peer_pkg_lower in package.lower():
                # Check if proposed version satisfies this constraint
                if not check_semver_constraint(proposed_version, peer_constraint):
                    return {
                        "incompatible": True,
                        "conflicting_package": other_pkg,
                        "conflicting_version": other_latest_version,
                        "reason": f"{other_pkg} {other_latest_version} requires {peer_pkg} {peer_constraint}, but {package} {proposed_version} does not satisfy this",
                        "docs": f"https://www.npmjs.com/package/{other_pkg}/v/{other_latest_version}",
                        "suggested_version": None,
                    }

    return None


def compare_versions(current: Dict[str, str], cdn_urls: Dict[str, List[str]]) -> Tuple[
    List[Tuple[str, str, str, str, List[str]]],
    List[Tuple[str, str, str, List[str], Dict]],
]:
    """
    Compare current versions with latest from npm registry
    Returns: (safe_updates, incompatible_updates) where:
        safe_updates: List of (package, current_version, target_version, latest_version, cdn_urls)
        incompatible_updates: List of (package, current_version, latest_version, cdn_urls, compat_info)
    """
    safe_updates = []
    incompatible_updates = []

    # First pass: fetch all latest versions
    print("  Fetching latest versions from npm...")
    latest_versions = {}

    for package in current.keys():
        latest = get_latest_version(package)
        if latest != "unknown":
            latest_versions[package] = latest

    print("")

    # Second pass: Try to update each package to LATEST, check compatibility
    # Build a dict of target versions (start with latest for all)
    target_versions = latest_versions.copy()

    # Check compatibility of all LATEST versions with each other
    for package, current_version in current.items():
        latest_version = latest_versions.get(package)

        if not latest_version or latest_version == current_version:
            target_versions[package] = current_version
            continue

        # Check if LATEST version is compatible with other LATEST versions
        compat_issue = check_compatibility(package, latest_version, current, latest_versions)

        if compat_issue:
            # Latest is incompatible, try same-major-version
            current_major = parse_version(current_version)[0]
            latest_major = parse_version(latest_version)[0]

            if latest_major > current_major:
                same_major_latest = get_latest_in_major_version(package, current_major)
                if same_major_latest and version_compare(same_major_latest, current_version) > 0:
                    # Try same-major update
                    compat_issue_same_major = check_compatibility(package, same_major_latest, current, latest_versions)

                    if not compat_issue_same_major:
                        # Same-major is compatible with latest versions of others
                        target_versions[package] = same_major_latest
                        continue

            # Try suggested version from peer deps
            suggested = compat_issue.get("suggested_version")
            if suggested and suggested != "unknown" and version_compare(suggested, current_version) > 0:
                target_versions[package] = suggested
            else:
                # No compatible upgrade available
                target_versions[package] = current_version

    # Third pass: Generate output with final target versions
    for package, current_version in current.items():
        latest_version = latest_versions.get(package)
        target_version = target_versions.get(package, current_version)
        package_urls = cdn_urls.get(package, [])

        if latest_version and latest_version != current_version:
            if target_version == current_version:
                # No upgrade available
                compat_issue = check_compatibility(package, latest_version, current, latest_versions)
                incompatible_updates.append(
                    (
                        package,
                        current_version,
                        latest_version,
                        package_urls,
                        compat_issue,
                    )
                )
                conflict = compat_issue["conflicting_package"] if compat_issue else "unknown"
                conflict_version = compat_issue.get("conflicting_version", "unknown") if compat_issue else "unknown"
                print(
                    f"⚠️   {package}: {current_version} - ❌  Already at max compatible (latest={latest_version} incompatible with {conflict} {conflict_version})"
                )
            else:
                # Can upgrade
                safe_updates.append(
                    (
                        package,
                        current_version,
                        target_version,
                        latest_version,
                        package_urls,
                    )
                )

                if target_version == latest_version:
                    print(f"📦  {package}: {current_version} → {target_version} ✅ (latest)")
                else:
                    current_major = parse_version(current_version)[0]
                    target_major = parse_version(target_version)[0]
                    print(
                        f"📦  {package}: {current_version} → {target_version} (max compatible, latest={latest_version})"
                    )
        else:
            print(f"✅  {package}: {current_version} (up to date)")

    return safe_updates, incompatible_updates


def generate_report(
    safe_updates: List[Tuple[str, str, str, str, List[str]]],
    incompatible_updates: List[Tuple[str, str, str, List[str], Dict]],
    current_versions: Dict[str, str],
) -> str:
    """Generate markdown report for GitHub issue"""
    if not safe_updates and not incompatible_updates:
        return "All CDN dependencies are up to date! ✅"

    report = "# 🔄 CDN Dependency Updates Available\n\n"

    # Incompatible updates section (warnings first)
    if incompatible_updates:
        report += "## ⚠️ Already at Maximum Compatible Version\n\n"
        report += "The following packages are already at their maximum compatible version:\n\n"
        report += "| Package | Current | Latest Available | Why Not Latest |\n"
        report += "|---------|---------|------------------|----------------|\n"

        for package, current, latest, cdn_urls, compat_info in incompatible_updates:
            conflict = compat_info["conflicting_package"]
            conflict_version = compat_info.get("conflicting_version", "unknown")
            report += (
                f"| `{package}` | {current} ✅ | ~~{latest}~~ | Incompatible with `{conflict}` {conflict_version} |\n"
            )

        report += "\n### Details\n\n"

        for package, current, latest, cdn_urls, compat_info in incompatible_updates:
            report += f"#### {package}\n\n"
            report += f"**Current version:** {current} (maximum compatible)\n\n"
            report += f"**Latest available:** {latest}\n\n"
            report += f"❌ **Why not latest:** {compat_info['reason']}\n\n"

        report += "---\n\n"

    # Safe updates section
    if safe_updates:
        report += "## ✅ Recommended Updates\n\n"
        report += "The following updates are safe and compatible:\n\n"
        report += "| Package | Current | Recommended | Latest Available |\n"
        report += "|---------|---------|-------------|------------------|\n"

        for package, current, target, latest, cdn_urls in safe_updates:
            if target == latest:
                report += f"| `{package}` | {current} | **{target}** | {latest} |\n"
            else:
                report += f"| `{package}` | {current} | **{target}** | ~~{latest}~~ ⚠️ |\n"

        report += "\n## 📦 CDN URL Updates\n\n"
        report += "Update the following CDN URLs in `templates/layout.html`:\n\n"

        # Single table for all packages
        # Store URL->hash mapping to avoid recalculating
        url_to_hash = {}

        report += "| Package | File Type | Current URL | Updated URL | Integrity Hash |\n"
        report += "|---------|-----------|-------------|-------------|----------------|\n"

        all_verified = True
        for package, current, target, latest, cdn_urls in safe_updates:
            for url in cdn_urls:
                file_type = "CSS" if url.endswith(".css") or "/css/" in url else "JS"

                # Find the updated URL using multiple strategies
                updated_url = find_updated_cdn_url(package, target, url)

                if not updated_url:
                    report += f"| `{package}` | {file_type} | `{url}` | ❌ Could not determine | N/A |\n"
                    all_verified = False
                    continue

                # Verify the updated URL exists
                print(f"  Verifying {package} {file_type}: {updated_url} ...")
                url_verified = verify_url_exists(updated_url)

                if not url_verified:
                    report += f"| `{package}` | {file_type} | `{url}` | `{updated_url}` | ❌ Failed |\n"
                    all_verified = False
                    continue

                # Calculate SRI hash for the updated URL
                print(f"  Calculating SRI hash for {package} {file_type}...")
                sri_hash = calculate_sri_hash(updated_url)

                if sri_hash:
                    url_to_hash[updated_url] = sri_hash
                    report += f"| `{package}` | {file_type} | `{url}` | `{updated_url}` | `{sri_hash}` |\n"
                else:
                    report += f"| `{package}` | {file_type} | `{url}` | `{updated_url}` | ❌ Hash failed |\n"
                    all_verified = False

        report += "\n"

        if all_verified:
            report += "✅ **All URLs verified and SRI hashes calculated**\n\n"
        else:
            report += "⚠️ **Some URLs failed verification or hash calculation** - check table above\n\n"

        report += "## 🔧 Ready-to-Use HTML Tags\n\n"
        report += "Copy and paste these tags directly into `templates/layout.html`:\n\n"

        for package, current, target, latest, cdn_urls in safe_updates:

            for url in cdn_urls:
                file_type = "CSS" if url.endswith(".css") or "/css/" in url else "JS"
                updated_url = find_updated_cdn_url(package, target, url)

                if not updated_url or updated_url not in url_to_hash:
                    report += f"**{file_type}:** ❌ Failed to generate tag\n\n"
                    continue

                sri_hash = url_to_hash[updated_url]

                report += f"**Replace {package} {file_type}:**\n"
                report += "```html\n"

                if file_type == "CSS":
                    report += f'<link rel="stylesheet" href="{updated_url}"\n'
                    report += f'      integrity="{sri_hash}"\n'
                    report += f'      crossorigin="anonymous">\n'
                else:
                    # JS file
                    report += f'<script src="{updated_url}"\n'
                    report += f'        integrity="{sri_hash}"\n'
                    report += f'        crossorigin="anonymous"></script>\n'

                report += "```\n\n"

        report += "### Update Steps\n\n"
        report += "1. Find the old tags in `templates/layout.html`\n"
        report += "2. Replace them with the corresponding tags from above\n"
        report += "3. Test the changes locally before deploying\n\n"

        report += "\n"

    return report


def main():
    layout_file = "templates/layout.html"

    print("Checking CDN dependency versions...\n")

    current_versions, cdn_urls = parse_layout_html(layout_file)
    print(f"Found {len(current_versions)} dependencies in {layout_file}\n")

    safe_updates, incompatible_updates = compare_versions(current_versions, cdn_urls)

    print(f"\n{'=' * 60}")

    total_updates = len(safe_updates) + len(incompatible_updates)

    if total_updates > 0:
        print(f"Found {total_updates} update(s) available:")
        print(f"  - {len(safe_updates)} safe update(s)")
        print(f"  - {len(incompatible_updates)} incompatible update(s)")

        report = generate_report(safe_updates, incompatible_updates, current_versions)

        # Write report to file for GitHub Actions
        with open("cdn-update-report.md", "w") as f:
            f.write(report)

        print("\nReport written to cdn-update-report.md")

        # Exit codes:
        # 0 = all up to date
        # 1 = safe updates available (create issue)
        # 2 = only incompatible updates (don't create issue)
        if safe_updates:
            print("✅ Safe updates available - will create/update GitHub issue")
            sys.exit(1)  # Trigger issue creation
        else:
            print("ℹ️  Only incompatible updates found - no issue will be created")
            sys.exit(2)  # Don't trigger issue creation
    else:
        print("All dependencies are up to date!")
        sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Error: Unhandled exception in CDN update check", file=sys.stderr)
        print(f"   {type(e).__name__}: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(3)  # Exit with distinct error code for unhandled exceptions
