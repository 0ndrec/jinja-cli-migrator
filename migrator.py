#!/usr/bin/env python3

import os
import sys
import yaml
import argparse
import shutil
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from jinja2 import Environment, FileSystemLoader, meta, Template
from jinja2.exceptions import TemplateError
import click
import re
from collections import defaultdict


@dataclass
class MigrationConfig:
    """Configuration for template migration"""
    source_dir: str
    target_dir: str
    new_base_template: str
    exclude_patterns: List[str]
    auto_preserve_blocks: bool
    auto_map_variables: bool


class JinjaMigrator:
    """Main migrator class for Jinja templates"""
    
    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        self.source_env = Environment(loader=FileSystemLoader(self.config.source_dir))
        self.target_env = Environment(loader=FileSystemLoader(self.config.target_dir))
        self.migration_log = []
        
        # Runtime mappings (discovered interactively or automatically)
        self.template_mappings = {}
        self.variable_mappings = {}
        self.block_mappings = {}
        self.discovered_variables = set()
        self.discovered_blocks = set()
        
    def _load_config(self, config_path: str) -> MigrationConfig:
        """Load migration configuration from YAML file"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)
            
            return MigrationConfig(
                source_dir=config_data['source_dir'],
                target_dir=config_data['target_dir'],
                new_base_template=config_data.get('new_base_template', 'base.html'),
                exclude_patterns=config_data.get('exclude_patterns', []),
                auto_preserve_blocks=config_data.get('auto_preserve_blocks', True),
                auto_map_variables=config_data.get('auto_map_variables', False)
            )
        except Exception as e:
            click.echo(f"❌ Error loading config: {e}", err=True)
            sys.exit(1)
    
    def _should_exclude(self, template_path: str) -> bool:
        """Check if template should be excluded from migration"""
        for pattern in self.config.exclude_patterns:
            if re.match(pattern, template_path):
                return True
        return False
    
    def _extract_template_info(self, template_path: str) -> Dict[str, Any]:
        """Extract information from existing template"""
        try:
            template = self.source_env.get_template(template_path)
            source = template.source
            
            # Parse template to get AST
            ast = self.source_env.parse(source)
            
            # Extract variables
            variables = meta.find_undeclared_variables(ast)
            
            # Extract blocks
            blocks = []
            for node in ast.find_all('Block'):
                blocks.append(node.name)
            
            # Extract extends/includes
            extends = None
            includes = []
            
            for node in ast.find_all('Extends'):
                if hasattr(node.template, 'value'):
                    extends = node.template.value
            
            for node in ast.find_all('Include'):
                if hasattr(node.template, 'value'):
                    includes.append(node.template.value)
            
            # Update discovered sets
            self.discovered_variables.update(variables)
            self.discovered_blocks.update(blocks)
            
            return {
                'variables': list(variables),
                'blocks': blocks,
                'extends': extends,
                'includes': includes,
                'source': source
            }
        except TemplateError as e:
            click.echo(f"⚠️  Error parsing template {template_path}: {e}")
            return {}
    
    def _interactive_template_mapping(self, template_path: str) -> str:
        """Interactively determine new template path"""
        if template_path in self.template_mappings:
            return self.template_mappings[template_path]
        
        click.echo(f"\n📄 Template: {template_path}")
        
        # Suggest automatic mapping
        suggested_path = self._suggest_template_path(template_path)
        
        choice = click.prompt(
            f"Choose action:\n"
            f"  1. Keep same path ({template_path})\n"
            f"  2. Use suggested path ({suggested_path})\n"
            f"  3. Enter custom path\n"
            f"  4. Skip this template\n"
            f"Choice", 
            type=int, 
            default=2
        )
        
        if choice == 1:
            new_path = template_path
        elif choice == 2:
            new_path = suggested_path
        elif choice == 3:
            new_path = click.prompt("Enter new path")
        else:
            return None  # Skip
        
        self.template_mappings[template_path] = new_path
        return new_path
    
    def _suggest_template_path(self, template_path: str) -> str:
        """Suggest new template path based on common patterns"""
        # Simple suggestions based on common patterns
        path = Path(template_path)
        
        # If it's in root, suggest moving to pages/
        if len(path.parts) == 1 and path.stem != 'base':
            return f"pages/{template_path}"
        
        # If it contains 'admin', suggest admin/ folder
        if 'admin' in template_path.lower():
            return f"admin/{path.name}"
        
        # If it contains 'user', suggest user/ folder  
        if 'user' in template_path.lower():
            return f"user/{path.name}"
        
        # Otherwise keep the same
        return template_path
    
    def _interactive_variable_mapping(self) -> None:
        """Interactively configure variable mappings"""
        if not self.discovered_variables or self.config.auto_map_variables:
            if self.config.auto_map_variables:
                self._auto_map_variables()
            return
        
        click.echo(f"\n🔧 Found {len(self.discovered_variables)} unique variables:")
        for var in sorted(self.discovered_variables):
            click.echo(f"  - {var}")
        
        if not click.confirm("\nWould you like to rename any variables?"):
            return
        
        for var in sorted(self.discovered_variables):
            new_name = click.prompt(
                f"Rename '{var}' to (press Enter to keep unchanged)", 
                default="", 
                show_default=False
            )
            if new_name and new_name != var:
                self.variable_mappings[var] = new_name
    
    def _auto_map_variables(self) -> None:
        """Automatically map variables based on common patterns"""
        auto_mappings = {
            'user_name': 'username',
            'user_email': 'email', 
            'page_title': 'title',
            'current_user': 'user',
            'nav_items': 'navigation'
        }
        
        for old_var, new_var in auto_mappings.items():
            if old_var in self.discovered_variables:
                self.variable_mappings[old_var] = new_var
    
    def _interactive_block_mapping(self) -> None:
        """Interactively configure block mappings"""
        if not self.discovered_blocks:
            return
        
        click.echo(f"\n🧱 Found {len(self.discovered_blocks)} unique blocks:")
        for block in sorted(self.discovered_blocks):
            click.echo(f"  - {block}")
        
        if self.config.auto_preserve_blocks:
            # Auto-map common blocks
            auto_block_mappings = {
                'content': 'main_content',
                'sidebar': 'aside_content', 
                'page_scripts': 'scripts',
                'page_styles': 'styles'
            }
            
            for old_block, new_block in auto_block_mappings.items():
                if old_block in self.discovered_blocks:
                    self.block_mappings[old_block] = new_block
                    
            click.echo("✅ Auto-mapped common blocks")
        
        if click.confirm("\nWould you like to customize block mappings?"):
            for block in sorted(self.discovered_blocks):
                current_mapping = self.block_mappings.get(block, block)
                new_name = click.prompt(
                    f"Map block '{block}' to", 
                    default=current_mapping
                )
                if new_name != block:
                    self.block_mappings[block] = new_name
    
    def _generate_new_template(self, template_info: Dict[str, Any], template_path: str) -> str:
        """Generate new template content based on discovered mappings"""
        lines = []
        
        # Add extends directive
        if self.config.new_base_template:
            lines.append(f"{{% extends '{self.config.new_base_template}' %}}")
            lines.append("")
        
        # Add blocks with mapped names
        original_blocks = self._extract_blocks_content(template_info['source'])
        
        for block_name, block_content in original_blocks.items():
            new_block_name = self.block_mappings.get(block_name, block_name)
            lines.append(f"{{% block {new_block_name} %}}")
            
            # Apply variable mappings to block content
            migrated_content = self._apply_variable_mappings(block_content)
            lines.append(migrated_content)
            
            lines.append("{% endblock %}")
            lines.append("")
        
        # Add migration comments
        lines.append("<!-- Migrated template -->")
        lines.append(f"<!-- Original: {template_path} -->")
        lines.append(f"<!-- Variables: {', '.join(template_info.get('variables', []))} -->")
        if self.variable_mappings:
            lines.append(f"<!-- Variable mappings: {self.variable_mappings} -->")
        
        return "\n".join(lines)
    
    def _extract_blocks_content(self, source: str) -> Dict[str, str]:
        """Extract block contents from template source"""
        blocks = {}
        
        # Improved regex pattern to extract blocks with proper nesting
        block_pattern = r'{%\s*block\s+(\w+)\s*%}(.*?){%\s*endblock\s*(?:\s+\1)?\s*%}'
        matches = re.finditer(block_pattern, source, re.DOTALL)
        
        for match in matches:
            block_name = match.group(1)
            block_content = match.group(2).strip()
            blocks[block_name] = block_content
        
        return blocks
    
    def _apply_variable_mappings(self, content: str) -> str:
        """Apply variable name mappings to content"""
        for old_var, new_var in self.variable_mappings.items():
            # Replace variable references in various Jinja contexts
            patterns = [
                rf'\b{re.escape(old_var)}\b',  # Simple variable
                rf'{{\s*{re.escape(old_var)}\s*}}',  # Template variable
                f'{{% [^%]*\\b{re.escape(old_var)}\\b[^%]*%}}',  # In control structures
            ]
            
            for pattern in patterns:
                content = re.sub(pattern, new_var, content)
        
        return content
    
    def discover_templates(self) -> List[str]:
        """Discover all templates in source directory"""
        templates = []
        source_path = Path(self.config.source_dir)
        
        for template_file in source_path.rglob("*.html"):
            relative_path = template_file.relative_to(source_path)
            templates.append(str(relative_path))
        
        return sorted(templates)
    
    def analyze_all_templates(self, templates: List[str]) -> None:
        """Analyze all templates to discover variables and blocks"""
        click.echo("🔍 Analyzing templates...")
        
        for template_path in templates:
            if not self._should_exclude(template_path):
                self._extract_template_info(template_path)
        
        click.echo(f"Found {len(self.discovered_variables)} variables and {len(self.discovered_blocks)} blocks")
    
    def configure_mappings_interactively(self) -> None:
        """Configure all mappings interactively"""
        click.echo("\n⚙️  Configuring Migration Mappings")
        click.echo("=" * 40)
        
        # Configure variable mappings
        self._interactive_variable_mapping()
        
        # Configure block mappings  
        self._interactive_block_mapping()
        
        # Show summary
        if self.variable_mappings:
            click.echo(f"\n📝 Variable mappings: {len(self.variable_mappings)}")
            for old, new in self.variable_mappings.items():
                click.echo(f"  {old} → {new}")
        
        if self.block_mappings:
            click.echo(f"\n🧱 Block mappings: {len(self.block_mappings)}")
            for old, new in self.block_mappings.items():
                click.echo(f"  {old} → {new}")
    
    def migrate_template(self, template_path: str) -> bool:
        """Migrate a single template"""
        try:
            if self._should_exclude(template_path):
                click.echo(f"⏭️  Skipping excluded template: {template_path}")
                return True
            
            # Get target path interactively
            target_path = self._interactive_template_mapping(template_path)
            if target_path is None:
                click.echo(f"⏭️  Skipping template: {template_path}")
                return True
            
            click.echo(f"📄 Migrating: {template_path} → {target_path}")
            
            # Extract template information
            template_info = self._extract_template_info(template_path)
            if not template_info:
                return False
            
            # Generate new template content
            new_content = self._generate_new_template(template_info, template_path)
            
            # Write new template
            full_output_path = Path(self.config.target_dir) / target_path
            full_output_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(full_output_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            self.migration_log.append({
                'source': template_path,
                'target': target_path,
                'variables': template_info.get('variables', []),
                'blocks': template_info.get('blocks', [])
            })
            
            click.echo(f"✅ Migrated successfully")
            return True
            
        except Exception as e:
            click.echo(f"❌ Error migrating {template_path}: {e}")
            return False
    
    def generate_migration_report(self) -> str:
        """Generate migration report"""
        report_lines = [
            "# Jinja Template Migration Report",
            f"## Configuration",
            f"- Source Directory: {self.config.source_dir}",
            f"- Target Directory: {self.config.target_dir}", 
            f"- New Base Template: {self.config.new_base_template}",
            "",
            "## Applied Mappings",
        ]
        
        if self.variable_mappings:
            report_lines.extend([
                "### Variable Mappings",
                "| Original | New |",
                "|----------|-----|"
            ])
            for old, new in self.variable_mappings.items():
                report_lines.append(f"| {old} | {new} |")
            report_lines.append("")
        
        if self.block_mappings:
            report_lines.extend([
                "### Block Mappings", 
                "| Original | New |",
                "|----------|-----|"
            ])
            for old, new in self.block_mappings.items():
                report_lines.append(f"| {old} | {new} |")
            report_lines.append("")
        
        report_lines.append("## Migrated Templates")
        
        for entry in self.migration_log:
            report_lines.extend([
                f"### {entry['source']} → {entry['target']}",
                f"- Variables: {', '.join(entry['variables']) or 'None'}",
                f"- Blocks: {', '.join(entry['blocks']) or 'None'}",
                ""
            ])
        
        return "\n".join(report_lines)


@click.command()
@click.option('--config', '-c', default='migration_config.yaml',
              help='Path to migration configuration file')
@click.option('--dry-run', '-d', is_flag=True,
              help='Show what would be migrated without actually doing it')
@click.option('--template', '-t', multiple=True,
              help='Specific templates to migrate (can be used multiple times)')
@click.option('--report', '-r', is_flag=True,
              help='Generate migration report after completion')
@click.option('--auto', '-a', is_flag=True,
              help='Use automatic mappings without interaction')
def main(config: str, dry_run: bool, template: tuple, report: bool, auto: bool):
    """Interactive CLI tool for migrating Jinja templates"""
    
    click.echo("🚀 Jinja Template Migrator")
    click.echo("=" * 40)
    
    # Check if config file exists
    if not os.path.exists(config):
        click.echo(f"❌ Configuration file not found: {config}")
        if click.confirm("Would you like to create a sample configuration file?"):
            create_sample_config(config)
            click.echo(f"✅ Sample configuration created: {config}")
            click.echo("Please edit the configuration file and run the migrator again.")
        return
    
    # Initialize migrator
    try:
        migrator = JinjaMigrator(config)
    except Exception as e:
        click.echo(f"❌ Failed to initialize migrator: {e}")
        return
    
    # Discover templates
    if template:
        templates_to_migrate = list(template)
    else:
        templates_to_migrate = migrator.discover_templates()
    
    if not templates_to_migrate:
        click.echo("⚠️  No templates found to migrate.")
        return
    
    # Analyze all templates first
    migrator.analyze_all_templates(templates_to_migrate)
    
    # Configure mappings (interactive or automatic)
    if not auto:
        migrator.configure_mappings_interactively()
    else:
        click.echo("🤖 Using automatic mappings")
        migrator._auto_map_variables()
        if migrator.config.auto_preserve_blocks:
            migrator._interactive_block_mapping()  # This will auto-map
    
    click.echo(f"\nFound {len(templates_to_migrate)} template(s) to migrate:")
    for t in templates_to_migrate:
        click.echo(f"  - {t}")
    
    if dry_run:
        click.echo("\n🔍 DRY RUN MODE - No files will be modified")
        for template_path in templates_to_migrate:
            target = migrator._suggest_template_path(template_path)
            click.echo(f"Would migrate: {template_path} → {target}")
        return
    
    # Confirm migration
    if not auto and not click.confirm(f"\nProceed with migration?"):
        click.echo("Migration cancelled.")
        return
    
    # Perform migration
    click.echo("\n🔄 Starting migration...")
    successful = 0
    failed = 0
    
    for template_path in templates_to_migrate:
        if auto:
            # Auto-assign target path
            migrator.template_mappings[template_path] = migrator._suggest_template_path(template_path)
        
        if migrator.migrate_template(template_path):
            successful += 1
        else:
            failed += 1
    
    # Summary
    click.echo(f"\n📊 Migration Summary:")
    click.echo(f"✅ Successful: {successful}")
    click.echo(f"❌ Failed: {failed}")
    
    # Generate report if requested
    if report and migrator.migration_log:
        report_content = migrator.generate_migration_report()
        report_path = "migration_report.md"
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_content)
        
        click.echo(f"📋 Migration report saved: {report_path}")


def create_sample_config(config_path: str):
    """Create a sample configuration file"""
    sample_config = {
        'source_dir': './templates',
        'target_dir': './new_templates', 
        'new_base_template': 'base.html',
        'exclude_patterns': [
            r'.*_backup\.html$',
            r'temp_.*\.html$'
        ],
        'auto_preserve_blocks': True,
        'auto_map_variables': False
    }
    
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(sample_config, f, default_flow_style=False, allow_unicode=True)


if __name__ == '__main__':
    main()