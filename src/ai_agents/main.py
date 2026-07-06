import argparse
import os
import sys

def find_java_files(directory: str) -> list[str]:
    """Recursively walks the directory and finds all files ending with .java."""
    java_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.java'):
                java_files.append(os.path.join(root, file))
    return java_files

def main():
    parser = argparse.ArgumentParser(description="Analyze Java source files to map out REST API calls.")
    parser.add_argument("directory", nargs="?", default=None, help="Path to the directory containing Java files")
    
    args = parser.parse_args()
    
    if not args.directory:
        print("Error: No directory path provided. Please specify a directory path.", file=sys.stderr)
        parser.print_help()
        sys.exit(1)
        
    directory = args.directory
    if not os.path.exists(directory):
        print(f"Error: The directory '{directory}' does not exist.", file=sys.stderr)
        sys.exit(1)
        
    if not os.path.isdir(directory):
        print(f"Error: '{directory}' is not a directory.", file=sys.stderr)
        sys.exit(1)
        
    java_files = find_java_files(directory)
    
    print(f"Total Java files found: {len(java_files)}")
    for path in sorted(java_files):
        print(path)

if __name__ == "__main__":
    main()
