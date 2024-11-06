import click
from bkbit.model_converters.sheets_converter import schema2model
from bkbit.model_converters.yaml2sheet_converter import yaml2cvs
from bkbit.data_translators.library_generation_translator import specimen2jsonld
from bkbit.data_translators.file_manifest_translator import filemanifest2jsonld
from bkbit.data_translators.specimen_metadata_translator import list_library_aliquot

@click.group()
def cli():
    """A CLI tool with multiple commands."""
    pass

# Add commands to the CLI group
cli.add_command(schema2model)
cli.add_command(yaml2cvs)
cli.add_command(specimen2jsonld)
cli.add_command(filemanifest2jsonld)
cli.add_command(list_library_aliquot)

if __name__ == '__main__':
    cli()
