# src/codegraphcontext/tools/graph_builder.py
import asyncio
import logging
from pathlib import Path
from typing import Any, Coroutine, Dict, Optional, Tuple
from datetime import datetime
import builtins

from ..core.database import DatabaseManager
from ..core.jobs import JobManager, JobStatus
from ..utils.debug_log import debug_log

# tree-sitter
from tree_sitter import Language, Parser
from tree_sitter_languages import get_language

logger = logging.getLogger(__name__)
debug_mode = 0


class TreeSitterParser:
    """A generic parser wrapper for a specific language using tree-sitter."""

    def __init__(self, language_name: str):
        self.language_name = language_name
        # get_language may raise — let caller handle or catch where appropriate
        self.language: Language = get_language(language_name)
        self.parser = Parser()
        self.parser.set_language(self.language)

        self.language_specific_parser = None
        if self.language_name == 'python':
            from .languages.python import PythonTreeSitterParser
            self.language_specific_parser = PythonTreeSitterParser(self)
        elif self.language_name == 'javascript':
            from .languages.javascript import JavascriptTreeSitterParser
            self.language_specific_parser = JavascriptTreeSitterParser(self)
        elif self.language_name == 'go':
            from .languages.go import GoTreeSitterParser
            self.language_specific_parser = GoTreeSitterParser(self)
        elif self.language_name == 'typescript':
            from .languages.typescript import TypescriptTreeSitterParser
            self.language_specific_parser = TypescriptTreeSitterParser(self)
        elif self.language_name == 'cpp':
            from .languages.cpp import CppTreeSitterParser
            self.language_specific_parser = CppTreeSitterParser(self)
        elif self.language_name == 'rust':
            from .languages.rust import RustTreeSitterParser
            self.language_specific_parser = RustTreeSitterParser(self)
        elif self.language_name == 'c':
            from .languages.c import CTreeSitterParser
            self.language_specific_parser = CTreeSitterParser(self)
        elif self.language_name == 'java':
            from .languages.java import JavaTreeSitterParser
            self.language_specific_parser = JavaTreeSitterParser(self)
        elif self.language_name == 'ruby':
            from .languages.ruby import RubyTreeSitterParser
            self.language_specific_parser = RubyTreeSitterParser(self)

    def parse(self, file_path: Path, is_dependency: bool = False, **kwargs) -> Dict:
        """Dispatches parsing to the language-specific parser."""
        if self.language_specific_parser:
            return self.language_specific_parser.parse(file_path, is_dependency, **kwargs)
        else:
            raise NotImplementedError(f"No language-specific parser implemented for {self.language_name}")


class GraphBuilder:
    """Module for building and managing the Neo4j code graph."""

    def __init__(self, db_manager: DatabaseManager, job_manager: JobManager, loop: asyncio.AbstractEventLoop):
        self.db_manager = db_manager
        self.job_manager = job_manager
        self.loop = loop
        self.driver = self.db_manager.get_driver()
        self.parsers = {
            '.py': TreeSitterParser('python'),
            '.ipynb': TreeSitterParser('python'),
            '.js': TreeSitterParser('javascript'),
            '.jsx': TreeSitterParser('javascript'),
            '.mjs': TreeSitterParser('javascript'),
            '.cjs': TreeSitterParser('javascript'),
            '.go': TreeSitterParser('go'),
            '.ts': TreeSitterParser('typescript'),
            '.tsx': TreeSitterParser('typescript'),
            '.cpp': TreeSitterParser('cpp'),
            '.h': TreeSitterParser('cpp'),
            '.hpp': TreeSitterParser('cpp'),
            '.rs': TreeSitterParser('rust'),
            '.c': TreeSitterParser('c'),
            '.java': TreeSitterParser('java'),
            '.rb': TreeSitterParser('ruby')
        }
        self.create_schema()

    def create_schema(self):
        """Create constraints and indexes in Neo4j."""
        with self.driver.session() as session:
            try:
                session.run("CREATE CONSTRAINT repository_path IF NOT EXISTS FOR (r:Repository) REQUIRE r.path IS UNIQUE")
                session.run("CREATE CONSTRAINT file_path IF NOT EXISTS FOR (f:File) REQUIRE f.path IS UNIQUE")
                session.run("CREATE CONSTRAINT directory_path IF NOT EXISTS FOR (d:Directory) REQUIRE d.path IS UNIQUE")
                session.run("CREATE CONSTRAINT function_unique IF NOT EXISTS FOR (f:Function) REQUIRE (f.name, f.file_path, f.line_number) IS UNIQUE")
                session.run("CREATE CONSTRAINT class_unique IF NOT EXISTS FOR (c:Class) REQUIRE (c.name, c.file_path, c.line_number) IS UNIQUE")
                session.run("CREATE CONSTRAINT variable_unique IF NOT EXISTS FOR (v:Variable) REQUIRE (v.name, v.file_path, v.line_number) IS UNIQUE")
                session.run("CREATE CONSTRAINT module_name IF NOT EXISTS FOR (m:Module) REQUIRE m.name IS UNIQUE")

                session.run("CREATE INDEX function_lang IF NOT EXISTS FOR (f:Function) ON (f.lang)")
                session.run("CREATE INDEX class_lang IF NOT EXISTS FOR (c:Class) ON (c.lang)")

                # Fulltext index — keep as-is but DB versions may differ
                session.run("""
                    CREATE FULLTEXT INDEX code_search_index IF NOT EXISTS 
                    FOR (n:Function|Class|Variable) 
                    ON EACH [n.name, n.source, n.docstring]
                """)
                logger.info("Database schema verified/created successfully")
            except Exception as e:
                logger.warning(f"Schema creation warning: {e}")

    def _pre_scan_for_imports(self, files: list[Path]) -> dict:
        """Dispatches pre-scan to the correct language-specific implementation."""
        imports_map = {}

        # Group files by language/extension
        files_by_lang = {}
        for file in files:
            if file.suffix in self.parsers:
                lang_ext = file.suffix
                files_by_lang.setdefault(lang_ext, []).append(file)

        # For each extension present, import and call pre-scan from language module
        # Using .get to avoid repeated imports and to keep code compact
        if '.py' in files_by_lang or '.ipynb' in files_by_lang:
            from .languages import python as python_lang_module
            py_files = files_by_lang.get('.py', []) + files_by_lang.get('.ipynb', [])
            imports_map.update(python_lang_module.pre_scan_python(py_files, self.parsers['.py']))

        if any(ext in files_by_lang for ext in ('.js', '.jsx', '.mjs', '.cjs')):
            from .languages import javascript as js_lang_module
            for ext in ('.js', '.jsx', '.mjs', '.cjs'):
                if ext in files_by_lang:
                    imports_map.update(js_lang_module.pre_scan_javascript(files_by_lang[ext], self.parsers[ext]))

        if '.go' in files_by_lang:
            from .languages import go as go_lang_module
            imports_map.update(go_lang_module.pre_scan_go(files_by_lang['.go'], self.parsers['.go']))

        if any(ext in files_by_lang for ext in ('.ts', '.tsx')):
            from .languages import typescript as ts_lang_module
            for ext in ('.ts', '.tsx'):
                if ext in files_by_lang:
                    imports_map.update(ts_lang_module.pre_scan_typescript(files_by_lang[ext], self.parsers[ext]))

        if any(ext in files_by_lang for ext in ('.cpp', '.h', '.hpp')):
            from .languages import cpp as cpp_lang_module
            for ext in ('.cpp', '.h', '.hpp'):
                if ext in files_by_lang:
                    imports_map.update(cpp_lang_module.pre_scan_cpp(files_by_lang[ext], self.parsers[ext]))

        if '.rs' in files_by_lang:
            from .languages import rust as rust_lang_module
            imports_map.update(rust_lang_module.pre_scan_rust(files_by_lang['.rs'], self.parsers['.rs']))

        if '.c' in files_by_lang:
            from .languages import c as c_lang_module
            imports_map.update(c_lang_module.pre_scan_c(files_by_lang['.c'], self.parsers['.c']))

        if '.java' in files_by_lang:
            from .languages import java as java_lang_module
            imports_map.update(java_lang_module.pre_scan_java(files_by_lang['.java'], self.parsers['.java']))

        if '.rb' in files_by_lang:
            from .languages import ruby as ruby_lang_module
            imports_map.update(ruby_lang_module.pre_scan_ruby(files_by_lang['.rb'], self.parsers['.rb']))

        return imports_map

    def add_repository_to_graph(self, repo_path: Path, is_dependency: bool = False):
        """Adds a repository node using its absolute path as the unique key."""
        repo_name = repo_path.name
        repo_path_str = str(repo_path.resolve())
        with self.driver.session() as session:
            session.run(
                """
                MERGE (r:Repository {path: $path})
                SET r.name = $name, r.is_dependency = $is_dependency
                """,
                path=repo_path_str,
                name=repo_name,
                is_dependency=is_dependency,
            )

    def add_file_to_graph(self, file_data: Dict, repo_name: str, imports_map: dict):
        logger.info("Executing add_file_to_graph with robust handling")
        file_path_str = str(Path(file_data.get('file_path')).resolve())
        file_name = Path(file_path_str).name
        is_dependency = file_data.get('is_dependency', False)

        # Determine repo path: prefer repo node if present, otherwise fallback to provided repo_path in file_data
        provided_repo_path = str(Path(file_data.get('repo_path', "")).resolve()) if file_data.get('repo_path') else None

        with self.driver.session() as session:
            # Try to find repository node
            repo_record = session.run("MATCH (r:Repository {path: $repo_path}) RETURN r.path as path", repo_path=provided_repo_path).single() if provided_repo_path else None
            repo_node_path = repo_record['path'] if repo_record else provided_repo_path or str(Path(repo_name).resolve())

            # Compute relative path safely
            try:
                relative_path = str(Path(file_path_str).relative_to(Path(repo_node_path)))
            except Exception:
                relative_path = file_name

            # Create/merge file node
            session.run("""
                MERGE (f:File {path: $path})
                SET f.name = $name, f.relative_path = $relative_path, f.is_dependency = $is_dependency
            """, path=file_path_str, name=file_name, relative_path=relative_path, is_dependency=is_dependency)

            # Build directory hierarchy relative to repo_node_path
            try:
                repo_path_obj = Path(repo_node_path)
            except Exception:
                repo_path_obj = Path(file_data.get('repo_path') or Path(file_path_str).parent)

            # create directories
            try:
                relative_path_to_file = Path(file_path_str).relative_to(repo_path_obj)
            except Exception:
                # fallback: use file name only
                relative_path_to_file = Path(file_name)

            parent_path = str(repo_path_obj)
            parent_label = 'Repository'

            for part in relative_path_to_file.parts[:-1]:
                current_path = Path(parent_path) / part
                current_path_str = str(current_path)

                session.run("""
                    MATCH (p:%s {path: $parent_path})
                    MERGE (d:Directory {path: $current_path})
                    SET d.name = $part
                    MERGE (p)-[:CONTAINS]->(d)
                """ % parent_label, parent_path=parent_path, current_path=current_path_str, part=part)

                parent_path = current_path_str
                parent_label = 'Directory'

            # Link file under parent
            session.run("""
                MATCH (p:%s {path: $parent_path})
                MATCH (f:File {path: $file_path})
                MERGE (p)-[:CONTAINS]->(f)
            """ % parent_label, parent_path=parent_path, file_path=file_path_str)

            # CONTAINS relationships for functions, classes, variables
            for item_data, label in [
                (file_data.get('functions', []), 'Function'),
                (file_data.get('classes', []), 'Class'),
                (file_data.get('variables', []), 'Variable')
            ]:
                for item in item_data:
                    # default cyclomatic_complexity
                    if label == 'Function' and 'cyclomatic_complexity' not in item:
                        item['cyclomatic_complexity'] = 1

                    # prepare props safely
                    props = dict(item)
                    # remove keys that are already in identifying triple to avoid param duplication if desired
                    # but we will pass props as-is for SET +=
                    session.run(f"""
                        MATCH (f:File {{path: $file_path}})
                        MERGE (n:{label} {{name: $name, file_path: $file_path, line_number: $line_number}})
                        SET n += $props
                        MERGE (f)-[:CONTAINS]->(n)
                    """, file_path=file_path_str, name=item.get('name'), line_number=item.get('line_number'), props=props)

                    if label == 'Function':
                        for arg_name in item.get('args', []):
                            session.run("""
                                MATCH (fn:Function {name: $func_name, file_path: $file_path, line_number: $line_number})
                                MERGE (p:Parameter {name: $arg_name, file_path: $file_path, function_line_number: $line_number})
                                MERGE (fn)-[:HAS_PARAMETER]->(p)
                            """, func_name=item.get('name'), file_path=file_path_str, line_number=item.get('line_number'), arg_name=arg_name)

            # nested function contains
            for item in file_data.get('functions', []):
                if item.get("context_type") == "function_definition":
                    session.run("""
                        MATCH (outer:Function {name: $context, file_path: $file_path})
                        MATCH (inner:Function {name: $name, file_path: $file_path, line_number: $line_number})
                        MERGE (outer)-[:CONTAINS]->(inner)
                    """, context=item.get("context"), file_path=file_path_str, name=item.get('name'), line_number=item.get('line_number'))

            # Handle imports
            for imp in file_data.get('imports', []):
                logger.info(f"Processing import: {imp}")
                lang = file_data.get('lang')
                # build safe parameters
                imp_name = imp.get('name') or imp.get('module') or imp.get('source') or imp.get('full_import_name') or imp.get('full_name')
                alias = imp.get('alias')
                full_import_name = imp.get('full_import_name') or imp.get('name')

                if lang == 'javascript':
                    module_name = imp.get('source') or imp.get('module') or imp_name
                    if not module_name:
                        continue
                    rel_props = {'imported_name': imp.get('name', '*')}
                    if alias:
                        rel_props['alias'] = alias

                    session.run("""
                        MATCH (f:File {path: $file_path})
                        MERGE (m:Module {name: $module_name})
                        MERGE (f)-[r:IMPORTS]->(m)
                        SET r += $props
                    """, file_path=file_path_str, module_name=module_name, props=rel_props)
                else:
                    # Python / others: create module and set optional properties safely
                    params = {'file_path': file_path_str, 'name': imp_name}
                    set_assignments = []
                    if alias:
                        params['alias'] = alias
                        set_assignments.append("m.alias = $alias")
                    if full_import_name:
                        params['full_import_name'] = full_import_name
                        set_assignments.append("m.full_import_name = $full_import_name")

                    set_clause = ", ".join(set_assignments) if set_assignments else ""
                    if set_clause:
                        cypher = f"""
                            MATCH (f:File {{path: $file_path}})
                            MERGE (m:Module {{name: $name}})
                            SET {set_clause}
                            MERGE (f)-[:IMPORTS]->(m)
                        """
                    else:
                        cypher = """
                            MATCH (f:File {path: $file_path})
                            MERGE (m:Module {name: $name})
                            MERGE (f)-[:IMPORTS]->(m)
                        """
                    session.run(cypher, **params)

            # CONTAINS relationship between class and functions (methods)
            for func in file_data.get('functions', []):
                if func.get('class_context'):
                    session.run("""
                        MATCH (c:Class {name: $class_name, file_path: $file_path})
                        MATCH (fn:Function {name: $func_name, file_path: $file_path, line_number: $func_line})
                        MERGE (c)-[:CONTAINS]->(fn)
                    """,
                    class_name=func.get('class_context'),
                    file_path=file_path_str,
                    func_name=func.get('name'),
                    func_line=func.get('line_number'))

            # Note: inheritance & calls handled in separate passes

    def _create_function_calls(self, session, file_data: Dict, imports_map: dict):
        """Create CALLS relationships with robust resolution logic."""
        caller_file_path = str(Path(file_data.get('file_path')).resolve())
        local_function_names = {func.get('name') for func in file_data.get('functions', []) if func.get('name')}
        # local_imports: alias or short name -> full import name
        local_imports = {}
        for imp in file_data.get('imports', []):
            name = imp.get('name') or imp.get('module') or imp.get('source') or imp.get('full_import_name')
            alias = imp.get('alias')
            if alias:
                local_imports[alias] = name
            elif name:
                short = name.split('.')[-1]
                local_imports[short] = name

        # builtins safe set
        builtin_names = set(dir(builtins))

        for call in file_data.get('function_calls', []):
            called_name = call.get('name')
            if not called_name:
                continue
            if called_name in builtin_names:
                continue

            resolved_path = None

            if call.get('inferred_obj_type'):
                obj_type = call.get('inferred_obj_type')
                possible_paths = imports_map.get(obj_type, [])
                if possible_paths:
                    resolved_path = possible_paths[0]
            else:
                full_name = call.get('full_name') or called_name
                lookup_name = full_name.split('.')[0] if '.' in full_name else called_name
                possible_paths = imports_map.get(lookup_name, [])

                if lookup_name in local_function_names:
                    resolved_path = caller_file_path
                elif len(possible_paths) == 1:
                    resolved_path = possible_paths[0]
                elif len(possible_paths) > 1 and lookup_name in local_imports:
                    full_import_name = local_imports[lookup_name]
                    for path in possible_paths:
                        if full_import_name.replace('.', '/') in path:
                            resolved_path = path
                            break

            if not resolved_path:
                if called_name in imports_map and imports_map[called_name]:
                    resolved_path = imports_map[called_name][0]
                else:
                    resolved_path = caller_file_path

            caller_context = call.get('context')
            if caller_context and isinstance(caller_context, (list, tuple)) and len(caller_context) == 3 and caller_context[0] is not None:
                caller_name, _, caller_line_number = caller_context
                session.run("""
                    MATCH (caller:Function {name: $caller_name, file_path: $caller_file_path, line_number: $caller_line_number})
                    MATCH (called:Function {name: $called_name, file_path: $called_file_path})
                    MERGE (caller)-[:CALLS {line_number: $line_number, args: $args, full_call_name: $full_call_name}]->(called)
                """,
                caller_name=caller_name,
                caller_file_path=caller_file_path,
                caller_line_number=caller_line_number,
                called_name=called_name,
                called_file_path=resolved_path,
                line_number=call.get('line_number'),
                args=call.get('args', []),
                full_call_name=call.get('full_name', called_name))
            else:
                session.run("""
                    MATCH (caller:File {path: $caller_file_path})
                    MATCH (called:Function {name: $called_name, file_path: $called_file_path})
                    MERGE (caller)-[:CALLS {line_number: $line_number, args: $args, full_call_name: $full_call_name}]->(called)
                """,
                caller_file_path=caller_file_path,
                called_name=called_name,
                called_file_path=resolved_path,
                line_number=call.get('line_number'),
                args=call.get('args', []),
                full_call_name=call.get('full_name', called_name))

    def _create_all_function_calls(self, all_file_data: list[Dict], imports_map: dict):
        """Create CALLS relationships for all files."""
        with self.driver.session() as session:
            for file_data in all_file_data or []:
                try:
                    self._create_function_calls(session, file_data, imports_map)
                except Exception as e:
                    logger.exception(f"Error creating function calls for {file_data.get('file_path')}: {e}")

    def _create_inheritance_links(self, session, file_data: Dict, imports_map: dict):
        """Create INHERITS relationships with robust resolution logic."""
        caller_file_path = str(Path(file_data.get('file_path')).resolve())
        local_class_names = {c.get('name') for c in file_data.get('classes', []) if c.get('name')}
        local_imports = {}
        for imp in file_data.get('imports', []):
            name = imp.get('name') or imp.get('module') or imp.get('full_import_name')
            alias = imp.get('alias')
            if alias:
                local_imports[alias] = name
            elif name:
                local_imports[name.split('.')[-1]] = name

        for class_item in file_data.get('classes', []):
            bases = class_item.get('bases') or []
            if not bases:
                continue

            for base_class_str in bases:
                if base_class_str == 'object':
                    continue

                resolved_path = None
                target_class_name = base_class_str.split('.')[-1]

                if '.' in base_class_str:
                    lookup_name = base_class_str.split('.')[0]
                    if lookup_name in local_imports:
                        full_import_name = local_imports[lookup_name]
                        possible_paths = imports_map.get(target_class_name, [])
                        for path in possible_paths:
                            if full_import_name and full_import_name.replace('.', '/') in path:
                                resolved_path = path
                                break
                else:
                    lookup_name = base_class_str
                    if lookup_name in local_class_names:
                        resolved_path = caller_file_path
                    elif lookup_name in local_imports:
                        full_import_name = local_imports[lookup_name]
                        possible_paths = imports_map.get(target_class_name, [])
                        for path in possible_paths:
                            if full_import_name and full_import_name.replace('.', '/') in path:
                                resolved_path = path
                                break
                    elif lookup_name in imports_map:
                        possible_paths = imports_map[lookup_name]
                        if len(possible_paths) == 1:
                            resolved_path = possible_paths[0]

                if resolved_path:
                    session.run("""
                        MATCH (child:Class {name: $child_name, file_path: $file_path})
                        MATCH (parent:Class {name: $parent_name, file_path: $resolved_parent_file_path})
                        MERGE (child)-[:INHERITS]->(parent)
                    """,
                    child_name=class_item.get('name'),
                    file_path=caller_file_path,
                    parent_name=target_class_name,
                    resolved_parent_file_path=resolved_path)

    def _create_all_inheritance_links(self, all_file_data: list[Dict], imports_map: dict):
        """Create INHERITS relationships for all classes."""
        with self.driver.session() as session:
            for file_data in all_file_data or []:
                try:
                    self._create_inheritance_links(session, file_data, imports_map)
                except Exception as e:
                    logger.exception(f"Error creating inheritance links for {file_data.get('file_path')}: {e}")

    def delete_file_from_graph(self, file_path: str):
        """Deletes a file and all its contained elements and relationships."""
        file_path_str = str(Path(file_path).resolve())
        with self.driver.session() as session:
            parents_res = session.run("""
                MATCH (f:File {path: $path})<-[:CONTAINS*]-(d:Directory)
                RETURN d.path as path ORDER BY d.path DESC
            """, path=file_path_str)
            parent_paths = [record["path"] for record in parents_res]

            session.run(
                """
                MATCH (f:File {path: $path})
                OPTIONAL MATCH (f)-[:CONTAINS]->(element)
                DETACH DELETE f, element
                """,
                path=file_path_str,
            )
            logger.info(f"Deleted file and its elements from graph: {file_path_str}")

            for path in parent_paths:
                session.run(""" 
                    MATCH (d:Directory {path: $path})
                    WHERE NOT (d)-[:CONTAINS]->()
                    DETACH DELETE d
                """, path=path)

    def delete_repository_from_graph(self, repo_path: str):
        """Deletes a repository and all its contents from the graph."""
        repo_path_str = str(Path(repo_path).resolve())
        with self.driver.session() as session:
            session.run("""MATCH (r:Repository {path: $path})
                          OPTIONAL MATCH (r)-[:CONTAINS*]->(e)
                          DETACH DELETE r, e""", path=repo_path_str)
            logger.info(f"Deleted repository and its contents from graph: {repo_path_str}")

    def update_file_in_graph(self, file_path: Path, repo_path: Path, imports_map: dict):
        """Updates a single file's nodes in the graph."""
        file_path_str = str(file_path.resolve())
        repo_name = repo_path.name

        self.delete_file_from_graph(file_path_str)

        if file_path.exists():
            file_data = self.parse_file(repo_path, file_path)

            if "error" not in file_data:
                self.add_file_to_graph(file_data, repo_name, imports_map)
                return file_data
            else:
                logger.error(f"Skipping graph add for {file_path_str} due to parsing error: {file_data['error']}")
                return None
        else:
            return {"deleted": True, "path": file_path_str}

    def parse_file(self, repo_path: Path, file_path: Path, is_dependency: bool = False) -> Dict:
        """Parses a file with the appropriate language parser and extracts code elements."""
        parser = self.parsers.get(file_path.suffix)
        if not parser:
            logger.warning(f"No parser found for file extension {file_path.suffix}. Skipping {file_path}")
            return {"file_path": str(file_path), "error": f"No parser for {file_path.suffix}"}

        debug_log(f"[parse_file] Starting parsing for: {file_path} with {parser.language_name} parser")
        try:
            is_notebook = file_path.suffix == '.ipynb'
            file_data = parser.parse(file_path, is_dependency, is_notebook=is_notebook)
            file_data['repo_path'] = str(repo_path)
            if debug_mode:
                debug_log(f"[parse_file] Successfully parsed: {file_path}")
            return file_data
        except Exception as e:
            logger.error(f"Error parsing {file_path} with {parser.language_name} parser: {e}")
            debug_log(f"[parse_file] Error parsing {file_path}: {e}")
            return {"file_path": str(file_path), "error": str(e)}

    def estimate_processing_time(self, path: Path) -> Optional[Tuple[int, float]]:
        """Estimate processing time and file count"""
        try:
            supported_extensions = set(self.parsers.keys())
            if path.is_file():
                if path.suffix in supported_extensions:
                    files = [path]
                else:
                    return 0, 0.0  # Not a supported file type
            else:
                all_files = path.rglob("*")
                files = [f for f in all_files if f.is_file() and f.suffix in supported_extensions]

            total_files = len(files)
            estimated_time = total_files * 0.05
            return total_files, estimated_time
        except Exception as e:
            logger.error(f"Could not estimate processing time for {path}: {e}")
            return None

    async def build_graph_from_path_async(
        self, path: Path, is_dependency: bool = False, job_id: str = None
    ):
        """Builds graph from a directory or file path."""
        try:
            if job_id:
                self.job_manager.update_job(job_id, status=JobStatus.RUNNING)

            self.add_repository_to_graph(path, is_dependency)
            repo_name = path.name

            supported_extensions = set(self.parsers.keys())
            all_files = path.rglob("*") if path.is_dir() else [path]
            files = [f for f in all_files if f.is_file() and f.suffix in supported_extensions]
            if job_id:
                self.job_manager.update_job(job_id, total_files=len(files))

            debug_log("Starting pre-scan to build imports map...")
            imports_map = self._pre_scan_for_imports(files)
            debug_log(f"Pre-scan complete. Found {len(imports_map)} definitions.")

            all_file_data = []
            processed_count = 0
            for file in files:
                if file.is_file():
                    if job_id:
                        self.job_manager.update_job(job_id, current_file=str(file))
                    repo_path = path.resolve() if path.is_dir() else file.parent.resolve()
                    file_data = self.parse_file(repo_path, file, is_dependency)
                    if "error" not in file_data:
                        self.add_file_to_graph(file_data, repo_name, imports_map)
                        all_file_data.append(file_data)
                    processed_count += 1
                    if job_id:
                        self.job_manager.update_job(job_id, processed_files=processed_count)
                    await asyncio.sleep(0.01)

            # Second-pass relationships
            self._create_all_inheritance_links(all_file_data, imports_map)
            self._create_all_function_calls(all_file_data, imports_map)

            if job_id:
                self.job_manager.update_job(job_id, status=JobStatus.COMPLETED, end_time=datetime.now())
        except Exception as e:
            error_message = str(e)
            logger.error(f"Failed to build graph for path {path}: {error_message}", exc_info=True)
            if job_id:
                # checking if the repo got deleted
                if any(token in error_message.lower() for token in ("no such file", "deleted", "not found")):
                    status = JobStatus.CANCELLED
                else:
                    status = JobStatus.FAILED

                self.job_manager.update_job(
                    job_id, status=status, end_time=datetime.now(), errors=[str(e)]
                )
