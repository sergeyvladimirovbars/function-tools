import os
import shutil
import sys
from distutils.util import (
    strtobool,
)
from os import (
    path,
)

import django
from django.conf import (
    settings,
)
from django.core.management.base import (
    CommandError,
)
from django.core.management.templates import (
    TemplateCommand,
)
from django.core.management.utils import (
    handle_extensions,
)
from django.template import (
    Context,
    Engine,
)
from django.utils.version import (
    get_docs_version,
)
from isort.api import (
    sort_code_string,
)
from isort.settings import (
    DEFAULT_CONFIG,
)

import function_tools
from function_tools.functions import (
    BaseFunction,
    LazyDelegateSavingPredefinedQueueFunction,
    LazySavingPredefinedQueueFunction,
)
from function_tools.management.consts import (
    PARAMETERS_DIALOG_WINDOW,
)
from function_tools.management.enums import (
    FunctionTypeEnum,
    ImplementationStrategyEnum,
)
from function_tools.management.strategies import (
    ImplementationStrategyFactory,
)
from function_tools.runners import (
    BaseRunner,
    LazySavingRunner,
)


class PatchedTemplateCommand(TemplateCommand):
    """
    Пропатченная команда для создания пакетов по шаблону
    """

    rewrite_template_suffixes = (
        ('.py-tpl', '.py'),
        ('.js-tpl', '.js'),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.name = None
        self.app_or_project = None
        self.paths_to_remove = None
        self.verbosity = None
        self.base_name = None
        self.base_subdir = None
        self.base_directory = None
        self.base_python_path = None
        self.camel_case_name = None
        self.camel_case_value = None
        self.extra_files = None
        self.extensions = None

        self._top_dir_path = None
        self._python_path_value = None

    def _get_conf_dir_parent_path(self):
        """
        Возвращает абсолютный путь директории, содержащей директорию conf с шаблонами внутри
        """
        return django.__path__[0]

    def _sort_imports(
        self,
        content: str,
    ):
        """
        Сортировка импортов при помощи isort
        """
        return sort_code_string(
            code=content,
            config=getattr(settings, 'ISORT_CONFIG') or DEFAULT_CONFIG,
        )

    def handle_template(self, template, subdir):
        """
        Determine where the app or project templates are.
        Use django.__path__[0] as the default because the Django install
        directory isn't known.
        """
        if template is None:
            return path.join(self._get_conf_dir_parent_path(), 'conf', subdir)
        else:
            if template.startswith('file://'):
                template = template[7:]

            expanded_template = path.expanduser(template)
            expanded_template = path.normpath(expanded_template)

            if path.isdir(expanded_template):
                return expanded_template

            if self.is_url(template):
                # downloads the file and returns the path
                absolute_path = self.download(template)
            else:
                absolute_path = path.abspath(expanded_template)

            if path.exists(absolute_path):
                return self.extract(absolute_path)

        raise CommandError(f'couldn\'t handle {self.app_or_project} template {template}.')

    def _make_top_dir(self, target, name):
        """
        Создание корневой директории
        """
        # if some directory is given, make sure it's nicely expanded
        if target is None:
            self._top_dir_path = path.join(os.getcwd(), name)
            try:
                os.makedirs(self._top_dir_path)
            except FileExistsError:
                raise CommandError(f'\'{self._top_dir_path}\' already exists')
            except OSError as e:
                raise CommandError(e)
        else:
            self._top_dir_path = os.path.abspath(path.expanduser(target))
            if not os.path.exists(self._top_dir_path):
                raise CommandError(
                    f'Destination directory \'{self._top_dir_path}\' does not exist, please create it first.'
                )

    def _prepare_top_module_python_path(self):
        """
        Формирование пути создаваемого пакета для дальнейшего использования в генерируемых импортах
        """
        top_dir_path = self._top_dir_path
        paths = [
            path_item
            for path_item in sys.path
            if path_item in top_dir_path
            # при запуске через django-admin в Python path добавляется директория, из которой запускается команда, что
            # искажает полноценный путь
            and f'{path_item}/{self.name}' != top_dir_path
        ]

        max_sys_path = max(paths)

        self._python_path_value = top_dir_path.split(f'{max_sys_path}/')[1]

        if '/' in self._python_path_value:
            self._python_path_value = self._python_path_value.replace('/', '.')

    def _prepare_extra_files(self, options):
        self.extra_files = []
        for file in options['files']:
            self.extra_files.extend(map(lambda x: x.strip(), file.split(',')))

    def _prepare_extensions(self, options):
        """
        Подготовка расширений файлов
        """
        self.extensions = tuple(handle_extensions(options['extensions']))

        if self.verbosity >= 2:
            self.stdout.write(
                f'Rendering {self.app_or_project} template files with extensions: {", ".join(self.extensions)}\n'
            )

            self.stdout.write(
                f'Rendering {self.app_or_project} template files with filenames: {", ".join(self.extra_files)}\n'
            )

    def _prepare_new_path_file(self, filename, relative_dir, options):
        """
        Подготовка пути генерируемого из шаблона файла
        """
        new_path = path.join(self._top_dir_path, relative_dir, filename.replace(self.base_name, self.name))

        for old_suffix, new_suffix in self.rewrite_template_suffixes:
            if new_path.endswith(old_suffix):
                new_path = new_path[:-len(old_suffix)] + new_suffix
                break  # Only rewrite once

        if path.exists(new_path):
            raise CommandError(
                f"{new_path} already exists, overlaying a project or app into an existing directory won't "
                f"replace conflicting files"
            )

        return new_path

    def _render_file(self, new_path, old_path, filename):
        # Only render the Python files, as we don't want to
        # accidentally render Django templates files
        if new_path.endswith(self.extensions) or filename in self.extra_files:
            with open(old_path, 'r', encoding='utf-8') as template_file:
                content = template_file.read()
            template = Engine().from_string(content)
            content = template.render(self.context)
            with open(new_path, 'w', encoding='utf-8') as new_file:
                if new_path.endswith('.py'):
                    content = self._sort_imports(content)

                new_file.write(content)
        else:
            shutil.copyfile(old_path, new_path)

        if self.verbosity >= 2:
            self.stdout.write("Creating %s\n" % new_path)
        try:
            shutil.copymode(old_path, new_path)
            self.make_writeable(new_path)
        except OSError:
            self.stderr.write(
                msg=(
                    f'Notice: Couldn\'t set permission bits on {new_path}. You\'re probably using an uncommon '
                    f'filesystem setup. No problem.'
                ),
                style_func=self.style.NOTICE,
            )

    def _create_package_by_template(self, options):
        template_dir = self.handle_template(options['template'], self.base_subdir)
        prefix_length = len(template_dir) + 1

        for root, dirs, files in os.walk(template_dir):

            path_rest = root[prefix_length:]
            relative_dir = path_rest.replace(self.base_name, self.name)
            if relative_dir:
                target_dir = path.join(self._top_dir_path, relative_dir)
                if not path.exists(target_dir):
                    os.mkdir(target_dir)

            for dirname in dirs[:]:
                if dirname.startswith('.') or dirname == '__pycache__':
                    dirs.remove(dirname)

            for filename in files:
                if filename.endswith(('.pyo', '.pyc', '.py.class')):
                    # Ignore some files as they cause various breakages.
                    continue
                old_path = path.join(root, filename)

                new_path = self._prepare_new_path_file(filename, relative_dir, options)

                self._render_file(new_path, old_path, filename)

    def _prepare_context(self, options):
        """
        Создание контекста
        """
        self.context = Context({
            **options,
            self.base_name: self.name,
            self.base_directory: self._top_dir_path,
            self.camel_case_name: self.camel_case_value,
            self.base_python_path: self._python_path_value,
            'docs_version': get_docs_version(),
            'django_version': django.__version__,
        }, autoescape=False)

    def _django_setup(self):
        """
        Инициализация Django для рендеринга шаблонов
        """
        if not settings.configured:
            settings.configure()
            django.setup()

    def _remove_paths(self):
        """
        Удаление помеченных файлов и директорий
        """
        if self.paths_to_remove:
            if self.verbosity >= 2:
                self.stdout.write('Cleaning up temporary files.\n')
            for path_to_remove in self.paths_to_remove:
                if path.isfile(path_to_remove):
                    os.remove(path_to_remove)
                else:
                    shutil.rmtree(path_to_remove)

    def _prepare_base_subdir_parameter(self, app_or_project, options):
        """
        Формирование параметра base_subdir
        """
        self.base_subdir = f'{app_or_project}_template'

    def _prepare_parameters(self, app_or_project, name, options):
        """
        Подготовка параметров для дальнейшей работы
        """
        self.name = name
        self.app_or_project = app_or_project
        self.paths_to_remove = []
        self.verbosity = options['verbosity']
        self.base_name = f'{app_or_project}_name'
        self._prepare_base_subdir_parameter(app_or_project, options)
        self.base_directory = f'{app_or_project}_directory'
        self.base_python_path = f'{app_or_project}_python_path'
        self.camel_case_name = f'camel_case_{app_or_project}_name'
        self.camel_case_value = ''.join(x for x in name.title() if x != '_')
        self.validate_name(name, app_or_project)

    def handle(self, app_or_project, name, target=None, **options):
        """
        Template command handler
        """
        self._prepare_parameters(app_or_project, name, options)
        self._make_top_dir(target=target, name=name)
        self._prepare_top_module_python_path()
        self._prepare_extra_files(options)
        self._prepare_extensions(options)
        self._prepare_context(options)
        self._create_package_by_template(options)
        self._remove_paths()


class Command(PatchedTemplateCommand):
    """
    Команда для создания Функции приложения
    """

    help = (
        """
        Команда для создания Функции приложения. В неименованном параметре передается наименование функции в стиле 
        camel_case. Переданное имя будет служить в качестве наименования пакета. Также будет произведено 
        преобразование переданного наименования к виду CamelCase, которое будет использоваться в наименовании классов.
        
        Существует несколько стратегий реализации Функции. Указать необходимую можно при помощи параметра --strategy.
        """
    )

    missing_args_message = "Вы должны указать наименование функции."

    def __init__(self, *args, **kwargs):
        self._strategies = self._prepare_strategies()

        super().__init__(*args, **kwargs)

    def add_arguments(self, parser):
        """
        Добавление параметров команды
        """
        super().add_arguments(parser)

        parser.add_argument(
            '--strategy',
            dest='strategy',
            action='store',
            type=lambda s: ImplementationStrategyEnum(int(s)),
            help=(
                """
                Стратегия реализации Функции.
                
                1 - простая Функция без отложенного сохранения;
                2 - Функция с отложенным сохранением запускаемым самой функцией
                3 - Функция с отложенным сохранением делегированным пускателю запуском 
                """
            ),
            default=ImplementationStrategyEnum.BASE_RUNNER_LAZY_SAVING_PREDEFINED_QUEUE_FUNCTION,
        )

        parser.add_argument(
            '--verbose_name',
            dest='function_verbose_name',
            action='store',
            type=str,
            help='Полное (человекочитаемое) название функции',
            default='Безымянная функция',
        )

        parser.add_argument(
            '--function_type',
            dest='function_type',
            action='store',
            type=lambda t: FunctionTypeEnum(int(t)),
            help='Тип исполнения функции. Возможные значения: 1 - синхронных, 2 - асинхронный',
            default=FunctionTypeEnum.SYNC,
        )

        parser.add_argument(
            '--is_parameterized',
            dest='is_parameterized',
            action='store',
            type=strtobool,
            help='Является ли функция параметризированной с необходимостью показа окна с параметрами пользователю?',
            default=False,
        )

    def _get_conf_dir_parent_path(self):
        """
        Возвращает абсолютный путь директории, содержащей директорию conf с шаблонами внутри
        """
        return function_tools.__path__[0]

    def _make_top_dir(self, target, name):
        super()._make_top_dir(target, name)

        if 'functions' not in self._top_dir_path:
            raise RuntimeError('Path for creating function must contain function directory')

    def _prepare_new_path_file(self, filename, relative_dir, options):
        """
        Подготовка пути генерируемого из шаблона файла
        """
        new_path = super()._prepare_new_path_file(filename, relative_dir, options)

        if PARAMETERS_DIALOG_WINDOW in new_path:
            if options['is_parameterized']:
                new_path = new_path.replace(PARAMETERS_DIALOG_WINDOW, f'{self.camel_case_value}{PARAMETERS_DIALOG_WINDOW}')  # noqa
            else:
                new_path = None

        return new_path

    def _render_file(self, new_path, old_path, filename):
        if new_path:
            super()._render_file(new_path, old_path, filename)

    def _prepare_base_subdir_parameter(self, app_or_project, options):
        """
        Формирование параметра base_subdir
        """
        function_type = options['function_type']

        self.base_subdir = f'{app_or_project}_{function_type.name.lower()}_template'

    def _prepare_parameters(self, app_or_project, name, options):
        """
        Подготовка параметров для дальнейшей работы
        """
        super()._prepare_parameters(app_or_project, name, options)

        self.url_name = self.name.replace('_', '-')
        self.base_url_name = f'{app_or_project}_url_name'

    def _prepare_context(self, options):
        """
        Создание контекста
        """
        self.context = Context({
            **options,
            self.base_name: self.name,
            self.base_url_name: self.url_name,
            self.base_directory: self._top_dir_path,
            self.camel_case_name: self.camel_case_value,
            self.base_python_path: self._python_path_value,
            'docs_version': get_docs_version(),
            'django_version': django.__version__,
        }, autoescape=False)

    def _prepare_strategies(self):
        """
        Подготовка стратегий реализзации функций

        Returns:
            Возвращает словарь, состоящий из идентификатора стратегии реализации функции и списком со словарем
        """
        return {
            1: [BaseRunner, BaseFunction],
            2: [BaseRunner, LazySavingPredefinedQueueFunction],
            3: [LazySavingRunner, LazyDelegateSavingPredefinedQueueFunction],
        }

    def handle(self, **options):
        function_name = options.pop('name')
        target = options.pop('directory')

        enum_strategy = options.pop('strategy')

        strategy_factory = ImplementationStrategyFactory()
        strategy = strategy_factory.get_strategy_implementation(
            enum_strategy=enum_strategy,
        )

        options['strategy'] = strategy

        options['extensions'].extend([
            'md',
            'js',
        ])

        super().handle('function', function_name, target, **options)
