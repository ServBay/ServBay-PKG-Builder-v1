#!/usr/bin/env bash

strip_package_record_line() {
    local line="$1"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line#\#}"
    line="${line#"${line%%[![:space:]]*}"}"
    printf '%s\n' "$line"
}

is_active_package_record_line() {
    local line="$1"
    [[ "$line" =~ ^[[:space:]]*# ]] && return 1
    [[ "$line" =~ [^[:space:]] ]]
}

read_package_record_fields() {
    local line="$1"
    local fields=()

    line="$(strip_package_record_line "$line")"
    IFS=$'\t' read -r -a fields <<< "$line"
    PACKAGE_RECORD_NAME="${fields[0]:-}"
    PACKAGE_RECORD_VERSION="${fields[1]:-}"
    PACKAGE_RECORD_FILENAME="${fields[2]:-}"

    PACKAGE_RECORD_NAME="${PACKAGE_RECORD_NAME#"${PACKAGE_RECORD_NAME%%[![:space:]]*}"}"
    PACKAGE_RECORD_NAME="${PACKAGE_RECORD_NAME%"${PACKAGE_RECORD_NAME##*[![:space:]]}"}"
    PACKAGE_RECORD_VERSION="${PACKAGE_RECORD_VERSION#"${PACKAGE_RECORD_VERSION%%[![:space:]]*}"}"
    PACKAGE_RECORD_VERSION="${PACKAGE_RECORD_VERSION%"${PACKAGE_RECORD_VERSION##*[![:space:]]}"}"
    PACKAGE_RECORD_FILENAME="${PACKAGE_RECORD_FILENAME#"${PACKAGE_RECORD_FILENAME%%[![:space:]]*}"}"
    PACKAGE_RECORD_FILENAME="${PACKAGE_RECORD_FILENAME%"${PACKAGE_RECORD_FILENAME##*[![:space:]]}"}"
}

emit_package_record() {
    printf '%s\t%s\t%s\n' "$1" "$2" "$3"
}

find_active_package_record() {
    local package_file="$1"
    local filter_key="$2"
    local line name version filename

    while IFS= read -r line || [[ -n "$line" ]]; do
        is_active_package_record_line "$line" || continue
        read_package_record_fields "$line"
        name="$PACKAGE_RECORD_NAME"
        version="$PACKAGE_RECORD_VERSION"
        filename="$PACKAGE_RECORD_FILENAME"
        [[ -n "$name" && -n "$version" ]] || continue
        if [[ "$filter_key" == "${name}-${version}" ]]; then
            emit_package_record "$name" "$version" "$filename"
            return 0
        fi
    done < "$package_file"

    return 1
}

find_template_package_record() {
    local package_file="$1"
    local filter_key="$2"
    local line name version filename desired_version
    local best_name="" best_version="" best_filename=""
    local fallback_name="" fallback_version="" fallback_filename=""

    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ [^[:space:]] ]] || continue
        read_package_record_fields "$line"
        name="$PACKAGE_RECORD_NAME"
        version="$PACKAGE_RECORD_VERSION"
        filename="$PACKAGE_RECORD_FILENAME"
        [[ -n "$name" && -n "$version" ]] || continue
        [[ "$filter_key" == "${name}-"* ]] || continue

        desired_version="${filter_key#${name}-}"
        [[ -n "$desired_version" && "$desired_version" != "$filter_key" ]] || continue

        if [[ -n "$filename" && "$filename" != *"$version"* ]]; then
            continue
        fi

        fallback_name="$name"
        fallback_version="$desired_version"
        if [[ -n "$filename" ]]; then
            fallback_filename="${filename//$version/$desired_version}"
        else
            fallback_filename=""
        fi

        if [[ "$version" != *dev* && "$version" != *snapshot* && "$version" != *latest* ]]; then
            best_name="$fallback_name"
            best_version="$fallback_version"
            best_filename="$fallback_filename"
        fi
    done < "$package_file"

    if [[ -n "$best_name" ]]; then
        emit_package_record "$best_name" "$best_version" "$best_filename"
        return 0
    fi

    if [[ -n "$fallback_name" ]]; then
        emit_package_record "$fallback_name" "$fallback_version" "$fallback_filename"
        return 0
    fi

    return 1
}

emit_active_package_records() {
    local package_file="$1"
    local line name version filename

    while IFS= read -r line || [[ -n "$line" ]]; do
        is_active_package_record_line "$line" || continue
        read_package_record_fields "$line"
        name="$PACKAGE_RECORD_NAME"
        version="$PACKAGE_RECORD_VERSION"
        filename="$PACKAGE_RECORD_FILENAME"
        [[ -n "$name" && -n "$version" ]] || continue
        emit_package_record "$name" "$version" "$filename"
    done < "$package_file"
}

resolve_windows_package_records() {
    local package_file="$1"
    local package_filter="${2:-}"
    local unresolved=0
    local filter_key

    if [[ -z "$package_filter" ]]; then
        emit_active_package_records "$package_file"
        return 0
    fi

    IFS=',' read -r -a filter_keys <<< "$package_filter"
    for filter_key in "${filter_keys[@]}"; do
        filter_key="${filter_key#"${filter_key%%[![:space:]]*}"}"
        filter_key="${filter_key%"${filter_key##*[![:space:]]}"}"
        [[ -n "$filter_key" ]] || continue

        if find_active_package_record "$package_file" "$filter_key"; then
            continue
        fi

        if find_template_package_record "$package_file" "$filter_key"; then
            continue
        fi

        echo "No package record or same-package template found for ${filter_key}" >&2
        unresolved=1
    done

    return "$unresolved"
}
