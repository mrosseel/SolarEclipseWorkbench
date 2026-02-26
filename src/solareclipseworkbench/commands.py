import logging
import shlex
import subprocess

def execute_command(command: str) -> None:
    """ Executes a command.

    Args:
        - command: Command to execute, as a string.
    """

    logging.info(f"Executing command: {command}")

    try:
        subprocess.run(shlex.split(command), check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"An error occurred while executing the command: {e}")
        raise
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        raise


