# Java Test Extraction Guide

本文档总结了Java测试方法符号抽取的关键要点、已知问题及解决方案。

## 1. JUnit 5 测试注解

JUnit 5 支持多种测试注解，不仅仅是 `@Test`：

| 注解 | 用途 | 示例 |
|------|------|------|
| `@Test` | 基础测试方法 | `@Test void testAdd() {}` |
| `@ParameterizedTest` | 参数化测试，用不同参数多次运行 | `@ParameterizedTest @ValueSource(ints={1,2,3}) void test(int n) {}` |
| `@RepeatedTest` | 重复测试，执行N次 | `@RepeatedTest(5) void testRandom() {}` |
| `@TestFactory` | 动态测试，运行时生成测试用例 | `@TestFactory Stream<DynamicTest> tests() {}` |
| `@TestTemplate` | 测试模板，与扩展机制配合 | `@TestTemplate void templateTest() {}` |

### 配置模式 (`symbol_extraction_config.yml`)

```yaml
# @Test 基础模式
test_method: ["@Test\nvoid $_() {$$$}", "void\\s+(\\w+)\\s*\\("]
test_method_public: ["@Test\npublic void $_() {$$$}", "void\\s+(\\w+)\\s*\\("]
test_method_throws: ["@Test\nvoid $_() throws $_ {$$$}", "void\\s+(\\w+)\\s*\\("]

# @ParameterizedTest 模式 (注意：参数源注解可能在前或后)
parameterized_test: ["@ParameterizedTest\n@$_($$$)\nvoid $_($$$) {$$$}", ...]
parameterized_test_source_first: ["@$_($$$)\n@ParameterizedTest\nvoid $_($$$) {$$$}", ...]

# @RepeatedTest 模式
repeated_test: ["@RepeatedTest($$$)\nvoid $_() {$$$}", ...]

# @TestFactory 模式 (返回类型不是void)
test_factory: ["@TestFactory\n$_ $_() {$$$}", ...]

# @TestTemplate 模式
test_template: ["@TestTemplate\nvoid $_($$$) {$$$}", ...]
```

### 识别函数 (`_is_test_symbol()`)

```python
if language == "java":
    java_test_prefixes = (
        "test_method",        # @Test
        "parameterized_test", # @ParameterizedTest
        "repeated_test",      # @RepeatedTest
        "test_factory",       # @TestFactory
        "test_template",      # @TestTemplate
    )
    return sym_type.startswith(java_test_prefixes)
```

## 2. 需要注意的边界情况

### 2.1 测试方法变体

测试方法可能有多种形式：

```java
// 基础形式
@Test
void testBasic() { }

// public修饰符
@Test
public void testPublic() { }

// 带throws子句
@Test
void testThrows() throws Exception { }

// 带其他注解
@SuppressWarnings("unchecked")
@Test
void testWithAnnotation() { }

// 参数化测试 - 注解顺序可能不同
@ParameterizedTest
@MethodSource("data")
void testParam(String arg) { }

@MethodSource("data")
@ParameterizedTest  // 注解在后面也是合法的
void testParam2(String arg) { }
```

### 2.2 不应被识别为测试的情况

以下情况不应被识别为测试方法变更：

| 情况 | 示例 | 原因 |
|------|------|------|
| 辅助方法 | `static void assertEqualsBigPerson(...)` | 没有 @Test 注解 |
| 工具类 | `ZKTools.java`, `TestResponse.java` | 虽在test目录但不是测试类 |
| 类级别注释 | `/** @author xxx */` 删除 | 不在测试方法内部 |
| setup/teardown | `@BeforeEach void setUp()` | 不是测试方法 |

### 2.3 注释变更的处理

当前策略：**只检测代码变更，忽略纯注释变更**

原因：
- 注释不影响测试行为
- 减少噪声，聚焦于实际代码变更

如果需要检测注释变更，需要修改符号范围比较逻辑。

## 3. 文件路径过滤

### 3.1 测试文件识别

Java测试文件通常位于：
```
src/test/java/**/*.java
```

但需要注意：
- `src/test/java` 下也有非测试类（工具类、mock类、测试数据类）
- 只有带测试注解的方法才是测试方法

### 3.2 Test ID 格式

生成的测试ID格式为：
```
module::package.ClassName::methodName
```

示例：
```
dubbo-common::org.apache.dubbo.common.logger.LoggerTest::testAllLogMethod
```

## 4. 已修复的问题

### 4.1 @ParameterizedTest 不被识别 (2024-01)

**问题**：使用 `@ParameterizedTest` 注解的测试方法不被检测

**解决**：在配置中添加 `parameterized_test*` 模式，在 `_is_test_symbol()` 中添加对应前缀

**验证**：commit `71f7600` 的 `testAllLogMethod` 现在被正确识别

### 4.2 Commit hash被解析为整数 (2024-01)

**问题**：纯数字commit hash（如 `0871406`）被 `json.loads()` 解析为整数

**解决**：在 `load_commits_from_file()` 中添加类型检查

```python
data = json.loads(line)
if isinstance(data, dict):
    sha = data.get("sha")
else:
    # 不是字典，按纯文本处理
    commits.append(line.split()[0])
```

### 4.3 重复检测 (历史修复)

**问题**：同一测试被多个模式匹配导致重复

**解决**：返回结果前使用 `sorted(set(...))` 去重

### 4.4 pygit2 误报 (历史修复)

**问题**：pygit2在新代码插入前的已有方法被错误标记为modified

**解决**：添加内容比较验证，检查前后内容是否真的不同

## 5. 检测统计示例

Apache Dubbo 3.3.3 → 3.3.6 版本：

| 指标 | 数量 |
|------|------|
| 总commits | 134 |
| 有测试变更的commits | 29 |
| Added tests | 51 |
| Modified tests | 163 |
| Deleted tests | 3 |
| **总计** | **217** |

## 6. 扩展建议

### 6.1 支持更多框架

如果需要支持其他测试框架，需要添加对应模式：

- **JUnit 4**: `@Test` (不同包)、`@Ignore`
- **TestNG**: `@Test`、`@DataProvider`
- **Spock**: `def "test name"()` (Groovy)

### 6.2 增加上下文信息

可以考虑在输出中添加：
- 测试所属的类名
- 测试的参数信息（对于参数化测试）
- 关联的setup/teardown方法

## 7. 相关文件

- 配置文件: `deepcommit/preprocess/configs/symbol_extraction_config.yml`
- 抽取脚本: `deepcommit/preprocess/extract_symbol_changes.py`
- 输出目录: `DATA/github_data/preprocessed/{project}/patched_tests/`
